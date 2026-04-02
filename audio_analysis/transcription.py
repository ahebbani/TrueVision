import os
import threading
import time
from datetime import datetime
from typing import Optional

import soundfile as sf

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None  # type: ignore

# Transcription model is optional import to allow running without it
try:
    from faster_whisper import WhisperModel
except Exception:  # pragma: no cover
    WhisperModel = None  # type: ignore

# ESP32 serial audio is optional
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


class Recorder:
    """Simple WAV recorder using sounddevice in a background thread.

    Usage:
        rec = Recorder(sample_rate=16000)
        path = rec.start(record_dir, filename_prefix)
        ...
        final_path = rec.stop()
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._file: Optional[sf.SoundFile] = None
        self._stream: Optional[object] = None
        self.audio_path: Optional[str] = None

    def start(self, directory: str, filename_prefix: str = "meeting") -> str:
        if sd is None:
            raise RuntimeError(
                "sounddevice is not installed. Install it to use the local microphone recorder, "
                "or use audio_source='esp32-serial'."
            )

        os.makedirs(directory, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.audio_path = os.path.join(directory, f"{filename_prefix}_{ts}.wav")
        self._file = sf.SoundFile(self.audio_path, mode='w', samplerate=self.sample_rate, channels=self.channels, subtype='PCM_16')

        def _callback(indata, frames, time_info, status):
            if status:
                # Non-fatal; dropouts will be in the stream
                pass
            if self._stop.is_set():
                assert sd is not None
                raise sd.CallbackStop()
            if self._file is not None:
                self._file.write(indata)

        assert sd is not None
        self._stream = sd.InputStream(samplerate=self.sample_rate, channels=self.channels, callback=_callback)
        self._stream.start()
        return self.audio_path

    def stop(self) -> Optional[str]:
        self._stop.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._file is not None:
                self._file.flush()
                self._file.close()
        except Exception:
            pass
        path = self.audio_path
        # reset
        self._stream = None
        self._file = None
        self.audio_path = None
        self._stop.clear()
        return path


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


def create_recorder(audio_source: str = "sounddevice", serial_port: str = "/dev/serial0", 
                   serial_baud: int = 921600, sample_rate: int = 16000, 
                   channels: int = 1) -> Recorder:
    """Factory function to create appropriate recorder based on audio source.
    
    Args:
        audio_source: "sounddevice" for local mic, "esp32-serial" for ESP32 via UART
        serial_port: Serial port device (only used for esp32-serial)
        serial_baud: Baud rate (only used for esp32-serial)
        sample_rate: Audio sample rate
        channels: Number of audio channels
        
    Returns:
        Recorder instance (either standard Recorder or ESP32SerialRecorder)
    """
    if audio_source == "auto":
        # Prefer ESP32 UART stream if present; fall back to local sounddevice.
        if probe_esp32_uart_stream is not None:
            if probe_esp32_uart_stream(port=serial_port, baud_rate=serial_baud, timeout_sec=1.0):
                audio_source = "esp32-serial"
            else:
                audio_source = "sounddevice"
        else:
            audio_source = "sounddevice"

    if audio_source == "esp32-serial":
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
    
    else:  # Default to sounddevice
        return Recorder(sample_rate=sample_rate, channels=channels)
