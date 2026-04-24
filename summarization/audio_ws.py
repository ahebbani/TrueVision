from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None  # type: ignore[assignment,misc]

try:
    import soundfile as sf
except Exception:
    sf = None  # type: ignore[assignment]


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


@dataclass
class AudioServerConfig:
    whisper_model: str = os.environ.get("WHISPER_MODEL", "small")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "auto")
    whisper_compute_type: str = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
    caption_interval_sec: float = float(os.environ.get("CAPTION_INTERVAL_SEC", "0.7"))
    caption_window_sec: float = float(os.environ.get("CAPTION_WINDOW_SEC", "3.0"))
    caption_max_words: int = int(os.environ.get("CAPTION_MAX_WORDS", "30"))
    translation_source_languages_raw: str = os.environ.get(
        "TRANSLATION_SOURCE_LANGUAGES",
        "es,de",
    )
    translation_target_language: str = os.environ.get("TRANSLATION_TARGET_LANGUAGE", "en")
    translation_detection_min_probability: float = float(
        os.environ.get("TRANSLATION_DETECTION_MIN_PROBABILITY", "0.65")
    )
    sample_rate: int = 16000
    channels: int = 1
    bytes_per_sample: int = 2

    def translation_source_languages(self) -> tuple[str, ...]:
        return _parse_csv(self.translation_source_languages_raw)

    def is_multilingual_whisper_model(self) -> bool:
        return not self.whisper_model.strip().lower().endswith(".en")

    def translation_available(self) -> bool:
        return (
            self.translation_target_language.strip().lower() == "en"
            and self.is_multilingual_whisper_model()
            and bool(self.translation_source_languages())
        )


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


class AudioTranscriptionHandler:
    def __init__(self, cfg: Optional[AudioServerConfig] = None):
        self.cfg = cfg or AudioServerConfig()
        self._model: Optional[WhisperModel] = None  # type: ignore[assignment]
        self._model_lock = threading.Lock()
        self._sessions: Dict[int, _AudioSession] = {}

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            if WhisperModel is None:
                raise RuntimeError("faster-whisper is not installed on the server")
            requested_device = (self.cfg.whisper_device or "auto").strip().lower()
            requested_compute = (self.cfg.whisper_compute_type or "float16").strip().lower()
            candidates = []
            if requested_device == "cpu":
                candidates = [("cpu", self._cpu_compute_type(requested_compute))]
            elif requested_device == "cuda":
                candidates = [
                    ("cuda", requested_compute),
                    ("cpu", self._cpu_compute_type(requested_compute)),
                ]
            else:
                candidates = [
                    ("cuda", requested_compute),
                    ("cpu", self._cpu_compute_type(requested_compute)),
                ]
            for device, compute_type in candidates:
                try:
                    self._model = WhisperModel(
                        self.cfg.whisper_model,
                        device=device,
                        compute_type=compute_type,
                    )
                    return
                except ValueError as exc:
                    msg = str(exc).lower()
                    if "cuda support" not in msg and "compiled with cuda" not in msg:
                        raise
            raise RuntimeError("Unable to initialize faster-whisper")

    @staticmethod
    def _cpu_compute_type(requested_compute: str) -> str:
        if requested_compute in {"float16", "int8_float16"}:
            return "int8"
        return requested_compute or "int8"

    def start_session(
        self,
        session_key: int,
        person_id: Optional[int] = None,
        meeting_id: Optional[int] = None,
    ) -> None:
        self._sessions[session_key] = _AudioSession(
            session_key=session_key,
            person_id=person_id,
            meeting_id=meeting_id,
        )

    def end_session(self, session_key: int) -> Optional[_AudioSession]:
        return self._sessions.pop(session_key, None)

    def append_audio(self, session_key: int, data: bytes) -> None:
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        sess.buffer.extend(data)

    def _buffer_to_wav(self, buf: bytes) -> str:
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
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        detected_language = getattr(info, "language", None)
        if isinstance(detected_language, str):
            detected_language = detected_language.lower()
        else:
            detected_language = None
        detected_probability = getattr(info, "language_probability", None)
        if detected_probability is not None:
            detected_probability = float(detected_probability)
        return _TranscriptionResult(
            text=text,
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

    def _cache_language(self, sess: _AudioSession, result: _TranscriptionResult) -> bool:
        language = (result.language or "").strip().lower() or None
        if language is None or not self._should_cache_language(result.language_probability):
            return False
        changed = language != sess.detected_language
        sess.detected_language = language
        sess.detected_language_probability = result.language_probability
        sess.translation_enabled = self._translation_enabled_for_language(language)
        return changed

    def _transcribe_session_wav(self, sess: _AudioSession, wav_path: str) -> _TranscriptionResult:
        if sess.detected_language:
            task = "translate" if sess.translation_enabled else "transcribe"
            return self._transcribe_wav(wav_path, task=task, language=sess.detected_language)
        result = self._transcribe_wav(wav_path)
        language_changed = self._cache_language(sess, result)
        if language_changed and sess.translation_enabled and sess.detected_language:
            return self._transcribe_wav(wav_path, task="translate", language=sess.detected_language)
        return result

    def _transcribe_buffer(self, sess: _AudioSession, buf: bytes) -> _TranscriptionResult:
        wav_path = self._buffer_to_wav(buf)
        try:
            return self._transcribe_session_wav(sess, wav_path)
        finally:
            self._cleanup_wav(wav_path)

    def maybe_caption(self, session_key: int) -> Optional[dict]:
        sess = self._sessions.get(session_key)
        if sess is None:
            return None
        now = time.time()
        if (now - sess.last_caption_time) < self.cfg.caption_interval_sec:
            return None
        min_bytes = self._min_caption_bytes()
        if len(sess.buffer) < min_bytes:
            return None
        result = self._transcribe_buffer(sess, self._live_caption_buffer(sess))
        self._cache_language(sess, result)
        sess.last_caption_time = now
        words = result.text.split()
        caption = " ".join(words[-self.cfg.caption_max_words:])
        if not caption:
            return None
        return {
            "text": caption,
            "source_language": sess.detected_language if sess.translation_enabled else None,
        }

    def final_transcribe(self, session_key: int) -> str:
        sess = self._sessions.get(session_key)
        if sess is None:
            return ""
        min_bytes = self._min_caption_bytes()
        if len(sess.buffer) < min_bytes:
            return ""
        result = self._transcribe_buffer(sess, bytes(sess.buffer))
        self._cache_language(sess, result)
        sess.full_transcript = result.text
        return result.text