from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from audio_analysis.transcription import LiveTranscriptionResult, language_label


@dataclass
class CaptionConfig:
    interval_sec: float = 0.7
    max_words: int = 30


class LiveCaptioner:
    def __init__(self, transcriber, cfg: CaptionConfig):
        self.transcriber = transcriber
        self.cfg = cfg
        self._last_update: Dict[int, float] = {}
        self._captions: Dict[int, str] = {}
        self._caption_languages: Dict[int, Optional[str]] = {}

    def clear(self, pid: int) -> None:
        self._last_update.pop(pid, None)
        self._captions.pop(pid, None)
        self._caption_languages.pop(pid, None)

    def clear_all(self) -> None:
        self._last_update.clear()
        self._captions.clear()
        self._caption_languages.clear()

    def update(self, active_recorders: Dict[int, object], active_meetings: Dict[int, int], cursor) -> None:
        now = time.time()
        for pid, rec in list(active_recorders.items()):
            audio_path = getattr(rec, 'audio_path', None)
            if not audio_path:
                # No active file yet; skip
                continue
            last_ts = self._last_update.get(pid, 0.0)
            if (now - last_ts) < self.cfg.interval_sec:
                continue
            # If the recorder supports flushing in-progress audio to disk
            # (ESP32SerialRecorder), do so before transcribing so the file exists.
            flush = getattr(rec, 'flush_to_wav', None)
            if flush is not None:
                try:
                    flushed = flush()
                except Exception:
                    flushed = False
                # If flush returned False the buffer was empty — file wasn't written.
                # Skip transcription this cycle rather than erroring on a missing file.
                if not flushed:
                    continue
            # For sounddevice Recorder, the file is written continuously; confirm
            # it actually exists before handing to Whisper.
            if not os.path.exists(audio_path):
                continue
            try:
                live_transcribe = getattr(self.transcriber, 'transcribe_live', None)
                if live_transcribe is not None:
                    live_result = live_transcribe(audio_path)
                else:
                    live_result = LiveTranscriptionResult(text=self.transcriber.transcribe(audio_path))
                text_live = live_result.text
                self._last_update[pid] = now
                words = (text_live or '').strip().split()
                tail_txt = ' '.join(words[-self.cfg.max_words:])
                self._captions[pid] = tail_txt
                self._caption_languages[pid] = live_result.detected_language
                mid = active_meetings.get(pid)
                if mid is not None and text_live:
                    cursor.execute(
                        "UPDATE meetings SET transcript = ? WHERE id = ?",
                        (text_live, mid),
                    )
            except Exception as e:
                # Surface issues to stdout to aid debugging
                print(f"LiveCaptioner: transcription error for pid={pid}, path={audio_path}: {e}")

    def get_caption_for_present(self, presence_state: Dict[int, str]) -> Optional[str]:
        for pid, state in presence_state.items():
            if state == 'present' and pid in self._captions:
                return self._captions.get(pid)
        # fallback to any caption
        if self._captions:
            return next(iter(self._captions.values()))
        return None

    def get_display_caption_for_present(self, presence_state: Dict[int, str]) -> Optional[str]:
        for pid, state in presence_state.items():
            if state == 'present' and pid in self._captions:
                return self._format_display_caption(pid)
        if self._captions:
            pid = next(iter(self._captions.keys()))
            return self._format_display_caption(pid)
        return None

    def _format_display_caption(self, pid: int) -> Optional[str]:
        caption = self._captions.get(pid)
        if not caption:
            return caption
        language = self._caption_languages.get(pid)
        if not language or language == 'en':
            return caption
        label = language_label(language)
        if not label:
            return caption
        return f"({label}) {caption}"
