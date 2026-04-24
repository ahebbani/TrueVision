"""WebSocket endpoint for real-time audio streaming and transcription.

Protocol
--------
Client (Pi) → Server:
    binary frames : raw int16 PCM audio (16 kHz, mono)
    text   frames : JSON control messages
        {"type": "session_start", "session_key": <int>, "person_id": <int|null>, "meeting_id": <int|null>}
        {"type": "session_end",   "session_key": <int>}

Server → Client (Pi):
    text frames : JSON messages
        {"type": "caption",    "session_key": <int>, "text": "..."}
        {"type": "transcript", "session_key": <int>, "full_text": "..."}
        {"type": "result",     "session_key": <int>, "meeting_id": <int>,
                               "transcript": "...", "summary": "..."}
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

from server.config import ServerConfig

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore[assignment,misc]

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]


# ── Per-session audio accumulator ─────────────────────────────────────────


@dataclass
class _TranscriptionResult:
    text: str
    language: Optional[str] = None
    language_probability: Optional[float] = None

@dataclass
class _AudioSession:
    session_key: int
    person_id: Optional[int] = None
    meeting_id: Optional[int] = None
    buffer: bytearray = field(default_factory=bytearray)
    last_caption_time: float = 0.0
    full_transcript: str = ""
    detected_language: Optional[str] = None
    detected_language_probability: Optional[float] = None
    translation_enabled: bool = False
    last_source_language: Optional[str] = None


class AudioTranscriptionHandler:
    """Handles WebSocket audio sessions: accumulates PCM, runs Whisper
    periodically, and sends captions/transcripts back to the client."""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self._model: Optional[WhisperModel] = None  # type: ignore[assignment]
        self._model_lock = threading.Lock()
        self._sessions: Dict[int, _AudioSession] = {}
        self._effective_device = cfg.whisper_device
        self._effective_compute_type = cfg.whisper_compute_type
        self._translation_warning_logged = False
        self._warn_if_translation_unavailable()

    def _warn_if_translation_unavailable(self) -> None:
        if self.cfg.translation_available() or self._translation_warning_logged:
            return
        if self.cfg.translation_target_language.strip().lower() != "en":
            print(
                "[audio_ws] Translation target language is not supported; "
                "live captions will pass through original speech."
            )
            self._translation_warning_logged = True
            return
        if not self.cfg.is_multilingual_whisper_model():
            print(
                f"[audio_ws] Whisper model '{self.cfg.whisper_model}' is English-only; "
                "Spanish/German live translation is disabled until a multilingual model is used."
            )
            self._translation_warning_logged = True

    # ── Lazy model init ───────────────────────────────────────────────────

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            if WhisperModel is None:
                raise RuntimeError("faster-whisper is not installed on the server")
            for device, compute_type in self._candidate_model_configs():
                print(
                    f"[audio_ws] Loading Whisper model={self.cfg.whisper_model} "
                    f"device={device} compute={compute_type}"
                )
                try:
                    self._model = WhisperModel(
                        self.cfg.whisper_model,
                        device=device,
                        compute_type=compute_type,
                    )
                    self._effective_device = device
                    self._effective_compute_type = compute_type
                    self.cfg.whisper_device = device
                    self.cfg.whisper_compute_type = compute_type
                    return
                except ValueError as exc:
                    if not self._is_cuda_backend_error(exc):
                        raise
                    print(
                        f"[audio_ws] CUDA backend unavailable for faster-whisper: {exc}. "
                        "Falling back to CPU."
                    )
                    continue

            raise RuntimeError(
                "Unable to initialize faster-whisper with any supported device configuration"
            )

    def _candidate_model_configs(self) -> list[tuple[str, str]]:
        requested_device = (self.cfg.whisper_device or "auto").strip().lower()
        requested_compute = (self.cfg.whisper_compute_type or "float16").strip().lower()

        if requested_device == "cpu":
            return [("cpu", self._cpu_compute_type(requested_compute))]
        if requested_device == "cuda":
            return [
                ("cuda", requested_compute),
                ("cpu", self._cpu_compute_type(requested_compute)),
            ]

        return [
            ("cuda", requested_compute),
            ("cpu", self._cpu_compute_type(requested_compute)),
        ]

    @staticmethod
    def _cpu_compute_type(requested_compute: str) -> str:
        if requested_compute in {"float16", "int8_float16"}:
            return "int8"
        return requested_compute or "int8"

    @staticmethod
    def _is_cuda_backend_error(exc: ValueError) -> bool:
        msg = str(exc).lower()
        return "cuda support" in msg or "compiled with cuda" in msg

    # ── Session management ────────────────────────────────────────────────

    def start_session(self, session_key: int, person_id: Optional[int] = None,
                      meeting_id: Optional[int] = None) -> None:
        self._sessions[session_key] = _AudioSession(
            session_key=session_key,
            person_id=person_id,
            meeting_id=meeting_id,
        )
        print(f"[audio_ws] Session started: key={session_key} person={person_id} meeting={meeting_id}")

    def end_session(self, session_key: int) -> Optional[_AudioSession]:
        return self._sessions.pop(session_key, None)

    def has_session(self, session_key: int) -> bool:
        return session_key in self._sessions

    # ── Audio ingestion ───────────────────────────────────────────────────

    def append_audio(self, session_key: int, data: bytes) -> None:
        sess = self._sessions.get(session_key)
        if sess is None:
            # Auto-create a default session when audio arrives before session_start.
            self.start_session(session_key)
            sess = self._sessions[session_key]
        sess.buffer.extend(data)

    # ── Transcription ─────────────────────────────────────────────────────

    def _buffer_to_wav(self, buf: bytes) -> str:
        """Write raw PCM buffer to a temp WAV file and return its path."""
        if sf is None:
            raise RuntimeError("soundfile is not installed on the server")
        audio = np.frombuffer(buf, dtype=np.int16)
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        sf.write(path, audio, self.cfg.sample_rate, subtype="PCM_16")
        return path

    def _cleanup_wav(self, wav_path: str) -> None:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    def _transcribe_wav(
        self,
        wav_path: str,
        *,
        task: str = "transcribe",
        language: Optional[str] = None,
    ) -> _TranscriptionResult:
        self._ensure_model()
        assert self._model is not None
        kwargs = {
            "beam_size": 1,
            "condition_on_previous_text": False,
            "without_timestamps": True,
            "vad_filter": False,
        }
        if task != "transcribe":
            kwargs["task"] = task
        if language:
            kwargs["language"] = language
        segments, info = self._model.transcribe(wav_path, **kwargs)
        parts = [seg.text.strip() for seg in segments]
        detected_language = getattr(info, "language", None)
        detected_probability = getattr(info, "language_probability", None)
        if isinstance(detected_language, str):
            detected_language = detected_language.lower()
        if detected_probability is not None:
            detected_probability = float(detected_probability)
        return _TranscriptionResult(
            text=" ".join(p for p in parts if p),
            language=detected_language,
            language_probability=detected_probability,
        )

    def _min_caption_bytes(self) -> int:
        return int(0.5 * self.cfg.sample_rate * self.cfg.bytes_per_sample * self.cfg.channels)

    def _live_window_bytes(self) -> int:
        return int(
            self.cfg.caption_window_sec
            * self.cfg.sample_rate
            * self.cfg.bytes_per_sample
            * self.cfg.channels
        )

    def _live_caption_buffer(self, sess: _AudioSession) -> bytes:
        window_bytes = self._live_window_bytes()
        if window_bytes <= 0 or len(sess.buffer) <= window_bytes:
            return bytes(sess.buffer)
        return bytes(sess.buffer[-window_bytes:])

    def _should_cache_language(self, probability: Optional[float]) -> bool:
        if probability is None:
            return True
        return probability >= self.cfg.translation_detection_min_probability

    def _translation_enabled_for_language(self, language: Optional[str]) -> bool:
        if not language or not self.cfg.translation_available():
            return False
        return language in self.cfg.translation_source_languages()

    @staticmethod
    def _normalize_language(language: Optional[str]) -> Optional[str]:
        if not language:
            return None
        normalized = language.strip().lower()
        return normalized or None

    def _clear_cached_language(self, sess: _AudioSession) -> None:
        sess.detected_language = None
        sess.detected_language_probability = None
        sess.translation_enabled = False

    def _cache_language(self, sess: _AudioSession, result: _TranscriptionResult) -> bool:
        language = self._normalize_language(result.language)
        if language is None:
            return False
        # Only lock the session to a translation language once we have a
        # reasonably confident supported-language detection. Otherwise keep
        # detecting on future live windows instead of getting stuck on an
        # early English guess from a short utterance.
        if not self._translation_enabled_for_language(language):
            return False
        if not self._should_cache_language(result.language_probability):
            return False
        changed = language != sess.detected_language
        sess.detected_language = language
        sess.detected_language_probability = result.language_probability
        sess.translation_enabled = True
        return changed

    def _should_clear_cached_language(self, result: _TranscriptionResult) -> bool:
        language = self._normalize_language(result.language)
        if language is None or self._translation_enabled_for_language(language):
            return False
        return self._should_cache_language(result.language_probability)

    def _select_translation_language(
        self,
        sess: _AudioSession,
        result: _TranscriptionResult,
    ) -> Optional[str]:
        if self._cache_language(sess, result):
            return sess.detected_language
        if sess.translation_enabled and sess.detected_language:
            if self._should_clear_cached_language(result):
                self._clear_cached_language(sess)
                return None
            return sess.detected_language
        if self._should_clear_cached_language(result):
            self._clear_cached_language(sess)
        return None

    def _apply_translation(
        self,
        sess: _AudioSession,
        wav_path: str,
        detected_result: _TranscriptionResult,
    ) -> _TranscriptionResult:
        translation_language = self._select_translation_language(sess, detected_result)
        if not translation_language:
            return detected_result

        translated_result = self._transcribe_wav(
            wav_path,
            task="translate",
            language=translation_language,
        )
        translated_text = translated_result.text or detected_result.text
        if detected_result.language == translation_language:
            language_probability = detected_result.language_probability
        else:
            language_probability = sess.detected_language_probability
        return _TranscriptionResult(
            text=translated_text,
            language=translation_language,
            language_probability=language_probability,
        )

    def _transcribe_session_wav(self, sess: _AudioSession, wav_path: str) -> _TranscriptionResult:
        detected_result = self._transcribe_wav(wav_path)
        return self._apply_translation(sess, wav_path, detected_result)

    def _transcribe_buffer(self, sess: _AudioSession, buf: bytes) -> _TranscriptionResult:
        wav_path = self._buffer_to_wav(buf)
        try:
            return self._transcribe_session_wav(sess, wav_path)
        finally:
            self._cleanup_wav(wav_path)

    def maybe_caption(self, session_key: int) -> Optional[str]:
        """If enough time has elapsed, transcribe current buffer and return
        the tail caption text.  Returns None if not yet time."""
        sess = self._sessions.get(session_key)
        if sess is None:
            return None
        now = time.time()
        if (now - sess.last_caption_time) < self.cfg.caption_interval_sec:
            return None
        min_bytes = self._min_caption_bytes()
        if len(sess.buffer) < min_bytes:
            return None  # not enough audio yet

        live_buf = self._live_caption_buffer(sess)

        print(
            f"[audio_ws] Live captioning session {session_key} "
            f"from {len(live_buf)} live-window bytes ({len(sess.buffer)} total buffered)"
        )
        result = self._transcribe_buffer(sess, live_buf)
        text = result.text
        sess.last_source_language = result.language if self._translation_enabled_for_language(result.language) else None
        sess.last_caption_time = now
        sess.full_transcript = text
        # Return the tail of the latest translated or transcribed text.
        words = text.split()
        caption = " ".join(words[-self.cfg.caption_max_words:])
        print(
            f"[audio_ws] Live caption updated for session {session_key}: "
            f"{len(caption)} chars"
        )
        return caption

    def caption_source_language(self, session_key: int) -> Optional[str]:
        sess = self._sessions.get(session_key)
        if sess is None:
            return None
        return sess.last_source_language

    def final_transcribe(self, session_key: int) -> str:
        """Run a final full transcription on the complete session buffer."""
        sess = self._sessions.get(session_key)
        if sess is None or len(sess.buffer) == 0:
            return ""
        min_bytes = self._min_caption_bytes()
        if len(sess.buffer) < min_bytes:
            return ""
        print(
            f"[audio_ws] Final transcription for session {session_key} "
            f"with {len(sess.buffer)} buffered bytes"
        )
        result = self._transcribe_buffer(sess, bytes(sess.buffer))
        text = result.text
        sess.last_source_language = result.language if self._translation_enabled_for_language(result.language) else None
        sess.full_transcript = text
        print(
            f"[audio_ws] Final transcription complete for session {session_key}: "
            f"{len(text)} chars"
        )
        return text

    def summarize(self, transcript: str, previous_summary: str = "",
                  person_name: Optional[str] = None, max_chars: int = 140) -> str:
        """Generate a one-sentence summary using Ollama (if available)."""
        if not transcript.strip():
            return ""
        try:
            from summarization.ollama_client import OllamaConfig, generate_one_shot
            from summarization.prompting import build_one_sentence_summary_prompt
            from summarization.text import clamp_summary_one_sentence

            cfg = OllamaConfig()
            print(
                f"[audio_ws] Summarizing transcript for {person_name or 'unknown person'} "
                f"({len(transcript)} chars, max {max_chars})"
            )
            prompt = build_one_sentence_summary_prompt(
                transcript=transcript,
                previous_summary=previous_summary,
                person_name=person_name,
                max_chars=max_chars,
            )
            raw, _meta = generate_one_shot(prompt, cfg=cfg)
            summary = clamp_summary_one_sentence(raw, max_chars=max_chars)
            if not summary:
                summary = clamp_summary_one_sentence(transcript, max_chars=max_chars)
            print(
                f"[audio_ws] Summary complete for {person_name or 'unknown person'}: "
                f"{len(summary)} chars"
            )
            return summary
        except Exception as e:
            print(f"[audio_ws] Summarization failed: {e}")
            return ""


# ── Singleton handler shared across websocket connections ─────────────────

_handler: Optional[AudioTranscriptionHandler] = None


def get_handler(cfg: Optional[ServerConfig] = None) -> AudioTranscriptionHandler:
    global _handler
    if _handler is None:
        _handler = AudioTranscriptionHandler(cfg or ServerConfig())
    return _handler
