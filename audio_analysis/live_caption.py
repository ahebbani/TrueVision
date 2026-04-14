from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class CaptionConfig:
    interval_sec: float = 0.7
    max_words: int = 30
    window_sec: float = 3.0


class LiveCaptioner:
    def __init__(self, transcriber, cfg: CaptionConfig):
        self.transcriber = transcriber
        self.cfg = cfg
        self._last_update: Dict[int, float] = {}
        self._captions: Dict[int, str] = {}
        self._status: Dict[int, str] = {}
        self._job_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._generation: Dict[int, int] = {}
        self._inflight: set[int] = set()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="live-caption-worker",
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._job_queue.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)

    def clear(self, pid: int) -> None:
        self._last_update.pop(pid, None)
        self._captions.pop(pid, None)
        self._status.pop(pid, None)
        self._generation[pid] = self._generation.get(pid, 0) + 1
        self._inflight.discard(pid)

    def clear_all(self) -> None:
        self._last_update.clear()
        self._captions.clear()
        self._status.clear()
        for pid in list(self._generation.keys()):
            self._generation[pid] = self._generation.get(pid, 0) + 1
        self._inflight.clear()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            job = self._job_queue.get()
            if job is None:
                continue

            pid, generation, audio_path, meeting_id = job
            try:
                text_live = self.transcriber.transcribe(audio_path)
                words = (text_live or '').strip().split()
                tail_txt = ' '.join(words[-self.cfg.max_words:])
                self._result_queue.put((pid, generation, meeting_id, text_live, tail_txt, None))
            except Exception as e:
                self._result_queue.put((pid, generation, meeting_id, None, None, e))

    def _drain_results(self, cursor) -> None:
        while True:
            try:
                pid, generation, meeting_id, text_live, tail_txt, err = self._result_queue.get_nowait()
            except queue.Empty:
                break

            if self._generation.get(pid, 0) != generation:
                continue

            self._inflight.discard(pid)

            if err is not None:
                self._status[pid] = "Transcription error"
                print(f"LiveCaptioner: transcription error for pid={pid}: {err}")
                continue

            self._captions[pid] = tail_txt or ''
            self._status[pid] = "Listening..." if not tail_txt else "Captions live"
            if meeting_id is not None and text_live:
                cursor.execute(
                    "UPDATE meetings SET transcript = ? WHERE id = ?",
                    (text_live, meeting_id),
                )

    def update(self, active_recorders: Dict[int, object], active_meetings: Dict[int, int], cursor) -> None:
        self._drain_results(cursor)
        now = time.time()
        for pid, rec in list(active_recorders.items()):
            if pid in self._inflight:
                continue
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
                    flushed = flush(seconds=self.cfg.window_sec)
                except Exception:
                    flushed = False
                # If flush returned False the buffer was empty — file wasn't written.
                # Skip transcription this cycle rather than erroring on a missing file.
                if not flushed:
                    self._status[pid] = "Waiting for audio..."
                    continue
            # Confirm the audio file actually exists before handing to Whisper.
            if not os.path.exists(audio_path):
                self._status[pid] = "Waiting for audio..."
                continue
            self._last_update[pid] = now
            if not self._captions.get(pid):
                self._status[pid] = "Transcribing..."
            self._inflight.add(pid)
            self._job_queue.put((pid, self._generation.get(pid, 0), audio_path, active_meetings.get(pid)))

    def get_caption_for_present(self, presence_state: Dict[int, str]) -> Optional[str]:
        for pid, state in presence_state.items():
            if state == 'present' and pid in self._captions:
                return self._captions.get(pid)
        # fallback to any caption
        if self._captions:
            return next(iter(self._captions.values()))
        return None

    def get_status_for_present(self, presence_state: Dict[int, str]) -> Optional[str]:
        for pid, state in presence_state.items():
            if state == 'present' and pid in self._status:
                return self._status.get(pid)
        if self._status:
            return next(iter(self._status.values()))
        return None
