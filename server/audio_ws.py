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
import io
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
class _AudioSession:
    session_key: int
    person_id: Optional[int] = None
    meeting_id: Optional[int] = None
    buffer: bytearray = field(default_factory=bytearray)
    last_caption_time: float = 0.0
    full_transcript: str = ""


class AudioTranscriptionHandler:
    """Handles WebSocket audio sessions: accumulates PCM, runs Whisper
    periodically, and sends captions/transcripts back to the client."""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self._model: Optional[WhisperModel] = None  # type: ignore[assignment]
        self._model_lock = threading.Lock()
        self._sessions: Dict[int, _AudioSession] = {}

    # ── Lazy model init ───────────────────────────────────────────────────

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            if WhisperModel is None:
                raise RuntimeError("faster-whisper is not installed on the server")
            print(f"[audio_ws] Loading Whisper model={self.cfg.whisper_model} "
                  f"device={self.cfg.whisper_device} compute={self.cfg.whisper_compute_type}")
            self._model = WhisperModel(
                self.cfg.whisper_model,
                device=self.cfg.whisper_device,
                compute_type=self.cfg.whisper_compute_type,
            )

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

    def _transcribe_wav(self, wav_path: str) -> str:
        self._ensure_model()
        assert self._model is not None
        segments, _info = self._model.transcribe(wav_path, beam_size=1)
        parts = [seg.text.strip() for seg in segments]
        return " ".join(p for p in parts if p)

    def maybe_caption(self, session_key: int) -> Optional[str]:
        """If enough time has elapsed, transcribe current buffer and return
        the tail caption text.  Returns None if not yet time."""
        sess = self._sessions.get(session_key)
        if sess is None:
            return None
        now = time.time()
        if (now - sess.last_caption_time) < self.cfg.caption_interval_sec:
            return None
        min_bytes = int(0.5 * self.cfg.sample_rate * self.cfg.bytes_per_sample)
        if len(sess.buffer) < min_bytes:
            return None  # not enough audio yet

        wav_path = self._buffer_to_wav(bytes(sess.buffer))
        try:
            text = self._transcribe_wav(wav_path)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
        sess.last_caption_time = now
        sess.full_transcript = text
        # Return last 30 words as caption
        words = text.split()
        return " ".join(words[-30:])

    def final_transcribe(self, session_key: int) -> str:
        """Run a final full transcription on the complete session buffer."""
        sess = self._sessions.get(session_key)
        if sess is None or len(sess.buffer) == 0:
            return ""
        min_bytes = int(0.5 * self.cfg.sample_rate * self.cfg.bytes_per_sample)
        if len(sess.buffer) < min_bytes:
            return ""
        wav_path = self._buffer_to_wav(bytes(sess.buffer))
        try:
            text = self._transcribe_wav(wav_path)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
        sess.full_transcript = text
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
