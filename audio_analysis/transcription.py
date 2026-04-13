import os
import threading
import time
from datetime import datetime
from typing import Optional

# Transcription model is optional import to allow running without it
try:
    from faster_whisper import WhisperModel
except Exception:  # pragma: no cover
    WhisperModel = None  # type: ignore

# ESP32 serial audio
try:
    from audio_analysis.esp32_serial_audio import (
        ESP32SerialAudioReceiver,
        ESP32SerialRecorder,
        probe_esp32_uart_stream,
    )
except Exception:  # pragma: no cover
    ESP32SerialAudioReceiver = None  # type: ignore
    ESP32SerialRecorder = None  # type: ignore
    probe_esp32_uart_stream = None  # type: ignore


_shared_serial_receivers = {}
_shared_serial_receivers_lock = threading.Lock()


class Transcriber:
    def __init__(self, model_size: str = "tiny", device: str = "cpu", compute_type: str = "int8"):  # int8/int8_float16/float16/float32
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model: Optional[WhisperModel] = None

    def _ensure_model(self):
        if WhisperModel is None:
            raise RuntimeError("faster-whisper is not installed. Install it or disable transcription.")
        if self._model is None:
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)

    def transcribe(self, audio_path: str) -> str:
        self._ensure_model()
        assert self._model is not None
        segments, info = self._model.transcribe(audio_path, beam_size=1)
        text_parts = []
        for seg in segments:
            text_parts.append(seg.text.strip())
        return " ".join([t for t in text_parts if t])


def summarize_text(text: str, max_sentences: int = 5) -> str:
    """Very simple extractive summary: return up to N sentences.
    This avoids pulling additional dependencies. Can be replaced later.
    """
    if not text:
        return ""
    # naive split on periods; keep short
    raw = text.replace("\n", " ")
    parts = [p.strip() for p in raw.split(".") if p.strip()]
    if not parts:
        return text.strip()
    summary = ". ".join(parts[:max_sentences])
    if not summary.endswith("."):
        summary += "."
    return summary


def summarize_one_sentence(text: str, max_chars: int = 140) -> str:
    """Return a single short sentence summary.

    This is intentionally dependency-free and safe to run on-device.
    If you later add an LLM summarizer, keep this as a fallback.
    """
    if not text:
        return ""
    s = summarize_text(text, max_sentences=1).replace("\n", " ").strip()
    s = " ".join(s.split())
    if not s:
        return ""
    if len(s) <= max_chars:
        return s

    # Clamp length without cutting in the middle of a word.
    clipped = s[: max_chars + 1]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    clipped = clipped.rstrip(" .")
    return clipped + "…"


def create_recorder(serial_port: str = "/dev/serial0",
                   serial_baud: int = 921600, sample_rate: int = 16000,
                   channels: int = 1, **_kwargs) -> 'ESP32SerialRecorder':
    """Create an ESP32 serial audio recorder.

    Returns:
        ESP32SerialRecorder instance
    """
    if ESP32SerialAudioReceiver is None or ESP32SerialRecorder is None:
        raise RuntimeError(
            "ESP32 serial audio not available. Install pyserial: pip install pyserial"
        )

    # Reuse a single shared serial receiver per (port, baud). UART is a single stream.
    key = (serial_port, int(serial_baud))
    with _shared_serial_receivers_lock:
        receiver = _shared_serial_receivers.get(key)
        if receiver is None:
            receiver = ESP32SerialAudioReceiver(
                port=serial_port,
                baud_rate=serial_baud,
                buffer_seconds=60.0,
            )
            receiver.start()
            _shared_serial_receivers[key] = receiver
        else:
            if not receiver.is_receiving():
                receiver.start()

    # Create recorder wrapper
    recorder = ESP32SerialRecorder(
        serial_receiver=receiver,
        sample_rate=sample_rate,
        channels=channels
    )

    print(f"ESP32 Serial Audio: Recorder ready on {serial_port} at {serial_baud} baud")
    return recorder


def get_shared_receiver(
    serial_port: str = '/dev/serial0',
    serial_baud: int = 921600,
    **kwargs,
) -> 'ESP32SerialAudioReceiver':
    """Return the cached ESP32SerialAudioReceiver for the given port/baud,
    creating and starting one (with any extra kwargs) if it does not yet exist.

    Extra kwargs are forwarded to ESP32SerialAudioReceiver.__init__ only when
    creating a new instance — they are silently ignored if the receiver is
    already running.  Pass on_mode_change, on_marker, and on_diag_request
    here when initialising callbacks from main.py.
    """
    if ESP32SerialAudioReceiver is None:
        raise RuntimeError("ESP32 serial audio not available. Install pyserial.")

    key = (serial_port, int(serial_baud))
    with _shared_serial_receivers_lock:
        receiver = _shared_serial_receivers.get(key)
        if receiver is None:
            receiver = ESP32SerialAudioReceiver(
                port=serial_port,
                baud_rate=serial_baud,
                buffer_seconds=60.0,
                **kwargs,
            )
            receiver.start()
            _shared_serial_receivers[key] = receiver
        else:
            # Apply callbacks if the caller provided them and they are not yet set
            for attr in ('on_mode_change', 'on_marker', 'on_diag_request'):
                if attr in kwargs and getattr(receiver, attr, None) is None:
                    setattr(receiver, attr, kwargs[attr])
            if not receiver.is_receiving():
                receiver.start()
    return receiver
