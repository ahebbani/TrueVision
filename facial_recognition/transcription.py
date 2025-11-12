import os
import threading
import time
from datetime import datetime
from typing import Optional

import sounddevice as sd
import soundfile as sf

# Transcription model is optional import to allow running without it
try:
    from faster_whisper import WhisperModel
except Exception:  # pragma: no cover
    WhisperModel = None  # type: ignore


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
        self._stream: Optional[sd.InputStream] = None
        self.audio_path: Optional[str] = None

    def start(self, directory: str, filename_prefix: str = "meeting") -> str:
        os.makedirs(directory, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.audio_path = os.path.join(directory, f"{filename_prefix}_{ts}.wav")
        self._file = sf.SoundFile(self.audio_path, mode='w', samplerate=self.sample_rate, channels=self.channels, subtype='PCM_16')

        def _callback(indata, frames, time_info, status):
            if status:
                # Non-fatal; dropouts will be in the stream
                pass
            if self._stop.is_set():
                raise sd.CallbackStop()
            if self._file is not None:
                self._file.write(indata)

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
