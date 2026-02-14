from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CaptionSpeakerConfig:
    enabled: bool = True
    min_interval_sec: float = 1.2
    speak_only_new: bool = True
    min_new_words: int = 2
    max_chars: int = 220

    # Engine tuning (best-effort; not all engines support these)
    rate_wpm: int = 175
    volume: float = 1.0  # 0.0-1.0


class CaptionSpeaker:
    """Asynchronously speaks live captions.

    - Non-blocking: `submit()` just enqueues work.
    - De-dupes: optionally speaks only new words compared to last spoken.
    - Best-effort: prefers `pyttsx3` if installed, else falls back to `espeak-ng`/`espeak`.

    If no TTS backend is available, it becomes a no-op.
    """

    def __init__(self, cfg: CaptionSpeakerConfig):
        self.cfg = cfg
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_spoken: str = ""
        self._last_spoken_ts: float = 0.0
        self._backend = _select_backend()

        if not self.cfg.enabled:
            self._backend = None

        if self._backend is not None:
            self._thread = threading.Thread(target=self._run, name="CaptionSpeaker", daemon=True)
            self._thread.start()

    @property
    def available(self) -> bool:
        return self._backend is not None

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        try:
            # Unblock the queue
            self._q.put_nowait("")
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def submit(self, caption: str) -> None:
        if not self.cfg.enabled or self._backend is None:
            return

        txt = (caption or "").strip()
        if not txt:
            return

        now = time.time()
        if (now - self._last_spoken_ts) < float(self.cfg.min_interval_sec):
            return

        if self.cfg.speak_only_new:
            txt_to_speak = _only_new_tail(self._last_spoken, txt, min_new_words=int(self.cfg.min_new_words))
            if not txt_to_speak:
                return
        else:
            txt_to_speak = txt

        txt_to_speak = _sanitize(txt_to_speak, max_chars=int(self.cfg.max_chars))
        if not txt_to_speak:
            return

        # Best-effort: drop oldest if queue is full
        try:
            self._q.put_nowait(txt_to_speak)
        except queue.Full:
            try:
                _ = self._q.get_nowait()
            except Exception:
                pass
            try:
                self._q.put_nowait(txt_to_speak)
            except Exception:
                pass

        self._last_spoken = txt
        self._last_spoken_ts = now

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                txt = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break
            txt = (txt or "").strip()
            if not txt:
                continue
            try:
                assert self._backend is not None
                self._backend.speak(txt, rate_wpm=int(self.cfg.rate_wpm), volume=float(self.cfg.volume))
            except Exception:
                # Never crash the main loop because of speech.
                pass


def _sanitize(text: str, max_chars: int) -> str:
    t = " ".join(text.replace("\n", " ").split())
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0].strip() or t[:max_chars].strip()
    return t


def _only_new_tail(prev_full: str, new_full: str, min_new_words: int) -> str:
    prev_words = (prev_full or "").strip().split()
    new_words = (new_full or "").strip().split()
    if not new_words:
        return ""

    # Find longest common prefix (word-wise)
    i = 0
    while i < len(prev_words) and i < len(new_words) and prev_words[i] == new_words[i]:
        i += 1

    tail = new_words[i:]
    if len(tail) < int(min_new_words):
        return ""

    return " ".join(tail)


class _Backend:
    def speak(self, text: str, rate_wpm: int, volume: float) -> None:  # pragma: no cover
        raise NotImplementedError


class _Pyttsx3Backend(_Backend):
    def __init__(self):
        import pyttsx3  # type: ignore

        self._pyttsx3 = pyttsx3
        self._engine = pyttsx3.init()

    def speak(self, text: str, rate_wpm: int, volume: float) -> None:
        # These are best-effort; not all drivers accept them.
        try:
            self._engine.setProperty("rate", int(rate_wpm))
        except Exception:
            pass
        try:
            self._engine.setProperty("volume", float(volume))
        except Exception:
            pass

        self._engine.say(text)
        self._engine.runAndWait()


class _EspeakBackend(_Backend):
    def __init__(self, exe: str):
        self._exe = exe

    def speak(self, text: str, rate_wpm: int, volume: float) -> None:
        # espeak volume is 0-200; map 0-1 -> 0-200
        amp = max(0, min(200, int(float(volume) * 200)))
        # -s speed, -a amplitude
        cmd = [self._exe, "-s", str(int(rate_wpm)), "-a", str(amp), text]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _select_backend() -> Optional[_Backend]:
    # Optional env to force disable
    if os.environ.get("SPEAK_CAPTIONS", "").strip() in {"0", "false", "False", "no", "NO"}:
        # don't force-disable if user didn't set it; treat explicit 0/false as off
        pass

    try:
        import pyttsx3  # noqa: F401

        return _Pyttsx3Backend()
    except Exception:
        pass

    for exe in ("espeak-ng", "espeak"):
        if shutil.which(exe):
            return _EspeakBackend(exe)

    return None
