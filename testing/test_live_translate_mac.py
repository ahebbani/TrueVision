#!/usr/bin/env python3
"""Standalone live translation probe for macOS development.

This script is intentionally separate from the production runtime. It records
short rolling microphone windows on your Mac, runs faster-whisper live
transcription/translation, and prints what the model detects in real time.

Use it to answer two questions before debugging the production server:
1. Can the chosen Whisper model translate German/Spanish speech to English?
2. Is automatic language detection good enough, or only forced language hints work?

Examples:
  python testing/test_live_translate_mac.py --list-devices
  python testing/test_live_translate_mac.py --model small
  python testing/test_live_translate_mac.py --model small --expected-language de
  python testing/test_live_translate_mac.py --model small --expected-language es --window-sec 4
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _NP_IMPORT_ERROR = exc
else:
    _NP_IMPORT_ERROR = None

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover
    sd = None  # type: ignore[assignment]
    _SD_IMPORT_ERROR = exc
else:
    _SD_IMPORT_ERROR = None

try:
    import soundfile as sf
except Exception as exc:  # pragma: no cover
    sf = None  # type: ignore[assignment]
    _SF_IMPORT_ERROR = exc
else:
    _SF_IMPORT_ERROR = None


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audio_analysis.transcription import Transcriber  # noqa: E402


SUPPORTED_TRANSLATION_LANGUAGES = ("es", "de")
LANGUAGE_LABELS = {
    "de": "German",
    "es": "Spanish",
    "en": "English",
}


def language_label(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    return LANGUAGE_LABELS.get(language.strip().lower())


@dataclass
class ProbeResult:
    text: str
    detected_language: Optional[str]
    source_language: Optional[str]
    translated: bool
    language_probability: Optional[float]
    used_hint: Optional[str]


@dataclass
class _LiveTranscriptionResult:
    text: str
    detected_language: Optional[str]
    source_language: Optional[str]
    translated: bool
    language_probability: Optional[float]


def _normalize_language(language: Optional[str]) -> Optional[str]:
    if not language:
        return None
    normalized = language.strip().lower()
    return normalized or None


def _should_reuse_detected_language(
    language: Optional[str],
    probability: Optional[float],
    min_probability: float,
) -> bool:
    if language not in SUPPORTED_TRANSLATION_LANGUAGES:
        return False
    if probability is None:
        return True
    return probability >= min_probability


def _should_clear_cached_language(
    language: Optional[str],
    probability: Optional[float],
    min_probability: float,
) -> bool:
    if language is None or language in SUPPORTED_TRANSLATION_LANGUAGES:
        return False
    if probability is None:
        return False
    return probability >= min_probability


def _select_source_language(
    detected_language: Optional[str],
    probability: Optional[float],
    cached_language: Optional[str],
    min_probability: float,
) -> tuple[Optional[str], Optional[str]]:
    normalized_detected = _normalize_language(detected_language)
    normalized_cached = _normalize_language(cached_language)

    if _should_reuse_detected_language(normalized_detected, probability, min_probability):
        return normalized_detected, normalized_detected
    if _should_clear_cached_language(normalized_detected, probability, min_probability):
        return None, None
    if normalized_cached in SUPPORTED_TRANSLATION_LANGUAGES:
        return normalized_cached, normalized_cached
    return None, None


def _transcribe_probe(
    transcriber: Transcriber,
    audio_path: str,
    cached_language: Optional[str],
    min_probability: float,
) -> _LiveTranscriptionResult:
    transcriber._ensure_model()
    assert transcriber._model is not None

    kwargs = {
        "beam_size": 1,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "vad_filter": False,
    }

    segments, info = transcriber._model.transcribe(audio_path, **kwargs)
    parts = [seg.text.strip() for seg in segments]
    detected_language = getattr(info, "language", None)
    detected_language = _normalize_language(detected_language)
    language_probability = getattr(info, "language_probability", None)
    if language_probability is not None:
        language_probability = float(language_probability)

    translated = False
    text = " ".join(part for part in parts if part)
    _next_cached_language, source_language = _select_source_language(
        detected_language,
        language_probability,
        cached_language,
        min_probability,
    )
    if source_language in SUPPORTED_TRANSLATION_LANGUAGES:
        translate_kwargs = dict(kwargs)
        translate_kwargs["task"] = "translate"
        translate_kwargs["language"] = source_language
        translated_segments, _translated_info = transcriber._model.transcribe(audio_path, **translate_kwargs)
        translated_parts = [seg.text.strip() for seg in translated_segments]
        translated_text = " ".join(part for part in translated_parts if part)
        if translated_text:
            text = translated_text
            translated = True

    return _LiveTranscriptionResult(
        text=text,
        detected_language=source_language or detected_language,
        source_language=source_language,
        translated=translated,
        language_probability=language_probability,
    )


class RollingMicrophoneBuffer:
    def __init__(self, sample_rate: int, channels: int, max_seconds: float, device=None):
        if sd is None:
            raise RuntimeError(
                f"sounddevice is not available: {_SD_IMPORT_ERROR}"
            )
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.max_bytes = int(max_seconds * self.sample_rate * self.channels * 2)
        self.device = device
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream = None

    def start(self) -> None:
        def _callback(indata, frames, time_info, status):
            if status:
                print(f"[mic] status: {status}")
            chunk = indata.copy().tobytes()
            with self._lock:
                self._buffer.extend(chunk)
                if len(self._buffer) > self.max_bytes:
                    trim = len(self._buffer) - self.max_bytes
                    self._buffer = self._buffer[trim:]

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=_callback,
            device=self.device,
            blocksize=1024,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def last_seconds(self, seconds: float) -> bytes:
        bytes_needed = int(seconds * self.sample_rate * self.channels * 2)
        with self._lock:
            if bytes_needed <= 0 or len(self._buffer) <= bytes_needed:
                return bytes(self._buffer)
            return bytes(self._buffer[-bytes_needed:])


def _list_devices() -> int:
    if sd is None:
        print(f"ERROR: sounddevice import failed: {_SD_IMPORT_ERROR}")
        return 2
    devs = sd.query_devices()
    default_in, _default_out = sd.default.device
    print("Available input devices:")
    for idx, dev in enumerate(devs):
        in_ch = int(dev.get("max_input_channels", 0))
        if in_ch <= 0:
            continue
        mark = " [default]" if idx == default_in else ""
        print(f"  {idx:>3}: {dev.get('name')} (in={in_ch}){mark}")
    return 0


def _resolve_device(device_arg: Optional[str]):
    if sd is None:
        return None
    if not device_arg:
        default_in, _default_out = sd.default.device
        return int(default_in) if default_in is not None else None
    try:
        return int(device_arg)
    except Exception:
        pass
    needle = device_arg.lower()
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        if needle in str(dev.get("name", "")).lower():
            return idx
    raise RuntimeError(f"No input device matched '{device_arg}'")


def _write_temp_wav(audio_bytes: bytes, sample_rate: int, channels: int) -> str:
    if np is None:
        raise RuntimeError(f"numpy import failed: {_NP_IMPORT_ERROR}")
    if sf is None:
        raise RuntimeError(f"soundfile import failed: {_SF_IMPORT_ERROR}")
    audio = np.frombuffer(audio_bytes, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="tv_live_translate_")
    os.close(fd)
    sf.write(path, audio, sample_rate, subtype="PCM_16")
    return path


def _render_result(result: ProbeResult) -> str:
    parts = []
    primary_language = result.source_language or result.detected_language
    if primary_language:
        label = language_label(primary_language) or primary_language
        if result.language_probability is not None:
            parts.append(f"detected={label} ({result.language_probability:.2f})")
        else:
            parts.append(f"detected={label}")
    if result.source_language and result.detected_language and result.source_language != result.detected_language:
        raw_label = language_label(result.detected_language) or result.detected_language
        parts.append(f"raw_detected={raw_label}")
    if result.used_hint:
        hint_label = language_label(result.used_hint) or result.used_hint
        parts.append(f"hint={hint_label}")
    parts.append("translated=yes" if result.translated else "translated=no")
    parts.append(f"text={result.text or '(empty)'}")
    return " | ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone live translation probe for macOS")
    parser.add_argument("--list-devices", action="store_true", help="List microphone devices and exit")
    parser.add_argument("--device", default=os.environ.get("AUDIO_IN_DEVICE"), help="Input device index or substring match")
    parser.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "small"), help="Whisper model to use; use a multilingual model, not *.en")
    parser.add_argument("--whisper-device", default=os.environ.get("WHISPER_DEVICE", "cpu"), help="Whisper device, e.g. cpu or cuda")
    parser.add_argument("--compute-type", default=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"), help="faster-whisper compute type")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--window-sec", type=float, default=3.0, help="Rolling audio window to transcribe each cycle")
    parser.add_argument("--poll-sec", type=float, default=1.0, help="How often to run live transcription")
    parser.add_argument("--min-lock-probability", type=float, default=0.65, help="Minimum probability before auto-detected German/Spanish is reused as a hint")
    parser.add_argument(
        "--expected-language",
        default="auto",
        choices=["auto", "de", "es"],
        help="Force a German or Spanish source-language hint instead of relying on auto detection",
    )
    args = parser.parse_args()

    if args.list_devices:
        return _list_devices()

    if sd is None:
        print(f"ERROR: sounddevice import failed: {_SD_IMPORT_ERROR}")
        return 2
    if sf is None:
        print(f"ERROR: soundfile import failed: {_SF_IMPORT_ERROR}")
        return 2
    if np is None:
        print(f"ERROR: numpy import failed: {_NP_IMPORT_ERROR}")
        return 2
    if args.model.strip().lower().endswith(".en"):
        print("ERROR: English-only Whisper models (*.en) cannot translate German or Spanish.")
        print("Use a multilingual model such as small, base, or medium.")
        return 2

    try:
        device = _resolve_device(args.device)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    print("Starting live translation probe")
    print(f"  model={args.model}")
    print(f"  whisper_device={args.whisper_device}")
    print(f"  compute_type={args.compute_type}")
    print(f"  window_sec={args.window_sec}")
    print(f"  poll_sec={args.poll_sec}")
    print(f"  input_device={device}")
    if args.expected_language != "auto":
        forced_label = language_label(args.expected_language) or args.expected_language
        print(f"  expected_language={forced_label} (forced hint)")
    else:
        print("  expected_language=auto")
    print("Speak German or Spanish. Press Ctrl-C to stop.\n")

    transcriber = Transcriber(
        model_size=args.model,
        device=args.whisper_device,
        compute_type=args.compute_type,
    )
    buffer = RollingMicrophoneBuffer(
        sample_rate=args.sample_rate,
        channels=args.channels,
        max_seconds=max(args.window_sec + 2.0, 8.0),
        device=device,
    )

    locked_language: Optional[str] = None
    last_rendered: Optional[str] = None

    try:
        buffer.start()
        while True:
            time.sleep(args.poll_sec)
            audio_bytes = buffer.last_seconds(args.window_sec)
            min_bytes = int(0.5 * args.sample_rate * args.channels * 2)
            if len(audio_bytes) < min_bytes:
                continue

            wav_path = _write_temp_wav(audio_bytes, args.sample_rate, args.channels)
            try:
                if args.expected_language != "auto":
                    language_hint = args.expected_language
                else:
                    language_hint = locked_language

                live_result = _transcribe_probe(
                    transcriber,
                    wav_path,
                    language_hint,
                    args.min_lock_probability,
                )
            finally:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

            if args.expected_language == "auto":
                locked_language, _source_language = _select_source_language(
                    live_result.source_language or live_result.detected_language,
                    live_result.language_probability,
                    locked_language,
                    args.min_lock_probability,
                )

            probe_result = ProbeResult(
                text=(live_result.text or "").strip(),
                detected_language=live_result.detected_language,
                source_language=live_result.source_language,
                translated=bool(live_result.translated),
                language_probability=live_result.language_probability,
                used_hint=language_hint,
            )
            rendered = _render_result(probe_result)
            if rendered != last_rendered:
                print(rendered)
                last_rendered = rendered
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        buffer.stop()


if __name__ == "__main__":
    raise SystemExit(main())