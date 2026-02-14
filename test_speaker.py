#!/usr/bin/env python3
"""Prove audio output (speaker/headphones) is working (in theory).

This is a "chatty" diagnostics script like prove_esp32_packetization.py, but for
system audio output.

What it validates:
- Python can open an output audio device
- We can stream a known waveform for a known duration
- We can generate a WAV file for external playback tools (aplay/vlc/etc)

Examples:
  python test_speaker.py --list-devices
  python test_speaker.py --seconds 2
  python test_speaker.py --pattern sweep --seconds 4 --volume 0.2
  python test_speaker.py --device 3 --pattern stereo --seconds 3
  python test_speaker.py --wav-out out.wav --no-play

Notes:
- This does not *guarantee* you hear audio (mute/volume/mixer/BT routing/etc),
  but it proves the software path can generate and stream audio without errors.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple


try:
	import numpy as np
except Exception as e:  # pragma: no cover
	np = None  # type: ignore
	_NP_ERR = e
else:
	_NP_ERR = None

try:
	import sounddevice as sd
except Exception as e:  # pragma: no cover
	sd = None  # type: ignore
	_SD_ERR = e
else:
	_SD_ERR = None

try:
	import soundfile as sf
except Exception as e:  # pragma: no cover
	sf = None  # type: ignore
	_SF_ERR = e
else:
	_SF_ERR = None


@dataclass
class RunStats:
	sample_rate: int
	channels: int
	frames_total: int
	frames_written: int
	peak_abs: float
	seconds_target: float
	seconds_actual: float


def _list_devices() -> int:
	if sd is None:
		print("ERROR: sounddevice is not installed.")
		print("Install: pip install sounddevice")
		if _SD_ERR is not None:
			print(f"Import error: {_SD_ERR}")
		return 2

	devs = sd.query_devices()
	if not devs:
		print("No audio devices found.")
		return 1

	default_in, default_out = sd.default.device
	print("Available sound devices (sounddevice):")
	for i, d in enumerate(devs):
		io = []
		if int(d.get("max_input_channels", 0)) > 0:
			io.append(f"in={d.get('max_input_channels')}")
		if int(d.get("max_output_channels", 0)) > 0:
			io.append(f"out={d.get('max_output_channels')}")
		io_txt = ", ".join(io) if io else "(no channels?)"
		mark = ""
		if i == default_out:
			mark = "  [default out]"
		elif i == default_in:
			mark = "  [default in]"
		print(f"  {i:>3}: {d.get('name')}  ({io_txt}){mark}")

	print("\nTip: pick an output device with out>0.")
	return 0


def _pick_device(device: Optional[str]) -> Optional[int]:
	if sd is None:
		return None

	if device is None or device == "":
		# default output
		_default_in, default_out = sd.default.device
		return int(default_out) if default_out is not None else None

	# numeric index
	try:
		return int(device)
	except Exception:
		pass

	# match by substring
	devs = sd.query_devices()
	needle = device.lower()
	for i, d in enumerate(devs):
		name = str(d.get("name", "")).lower()
		if needle in name and int(d.get("max_output_channels", 0)) > 0:
			return int(i)

	return None


def _generate(pattern: str, seconds: float, sample_rate: int, channels: int, volume: float, freq: float) -> Tuple["np.ndarray", float]:
	assert np is not None

	n = max(1, int(round(seconds * sample_rate)))
	t = np.arange(n, dtype=np.float32) / float(sample_rate)

	amp = float(max(0.0, min(1.0, volume)))

	if pattern == "tone":
		x = np.sin(2.0 * math.pi * float(freq) * t)
	elif pattern == "beep":
		# 250ms on / 250ms off beeps
		gate = ((t * 4.0) % 2.0) < 1.0
		x = np.sin(2.0 * math.pi * float(freq) * t) * gate.astype(np.float32)
	elif pattern == "noise":
		rng = np.random.default_rng(0)
		x = rng.standard_normal(n).astype(np.float32)
		x = np.clip(x, -3.0, 3.0) / 3.0
	elif pattern == "sweep":
		# log-ish sweep from 200Hz to 2kHz
		f0, f1 = 200.0, 2000.0
		k = math.log(f1 / f0) / max(1e-6, seconds)
		inst_f = f0 * np.exp(k * t)
		phase = 2.0 * math.pi * np.cumsum(inst_f) / float(sample_rate)
		x = np.sin(phase)
	elif pattern == "stereo":
		# Left: 440Hz, Right: 660Hz
		left = np.sin(2.0 * math.pi * 440.0 * t)
		right = np.sin(2.0 * math.pi * 660.0 * t)
		if channels < 2:
			x = left
		else:
			y = np.stack([left, right], axis=1)
			y = (y * amp).astype(np.float32)
			return y, float(np.max(np.abs(y)))
	else:
		raise ValueError(f"Unknown pattern: {pattern}")

	x = (x * amp).astype(np.float32)

	if channels <= 1:	
		y = x.reshape(-1, 1)
	else:
		y = np.repeat(x.reshape(-1, 1), channels, axis=1)

	peak = float(np.max(np.abs(y))) if y.size else 0.0
	return y, peak


def _play(audio: "np.ndarray", device_index: Optional[int], sample_rate: int) -> Tuple[int, float]:
	assert sd is not None

	frames_written = 0
	start = time.time()

	def _callback(outdata, frames, time_info, status):
		nonlocal frames_written
		if status:
			# underrun/overrun warnings
			pass

		start_idx = frames_written
		end_idx = start_idx + frames
		chunk = audio[start_idx:end_idx]
		if len(chunk) < frames:
			outdata[: len(chunk)] = chunk
			outdata[len(chunk) :] = 0
			raise sd.CallbackStop()
		outdata[:] = chunk
		frames_written += frames

	with sd.OutputStream(
		samplerate=int(sample_rate),
		device=device_index,
		channels=int(audio.shape[1]),
		dtype="float32",
		callback=_callback,
		blocksize=1024,
	):
		while True:
			time.sleep(0.05)
			if frames_written >= int(audio.shape[0]):
				break

	elapsed = time.time() - start
	return int(frames_written), float(elapsed)


def main() -> int:
	p = argparse.ArgumentParser(description="Prove audio output works (speaker test)")
	p.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
	p.add_argument("--device", default=os.environ.get("AUDIO_OUT_DEVICE"), help="Output device index or substring match")
	p.add_argument("--sample-rate", type=int, default=int(os.environ.get("AUDIO_SAMPLE_RATE", "48000")))
	p.add_argument("--channels", type=int, default=int(os.environ.get("AUDIO_CHANNELS", "2")))
	p.add_argument("--seconds", type=float, default=2.0, help="Playback duration")
	p.add_argument("--pattern", default="beep", choices=["beep", "tone", "sweep", "noise", "stereo"], help="Test signal")
	p.add_argument("--freq", type=float, default=440.0, help="Tone/beep frequency")
	p.add_argument("--volume", type=float, default=float(os.environ.get("AUDIO_VOLUME", "0.25")), help="0.0-1.0")
	p.add_argument("--wav-out", default="", help="Also write a WAV file to this path")
	p.add_argument("--no-play", action="store_true", help="Do not play; only generate (and optionally write WAV)")
	args = p.parse_args()

	if args.list_devices:
		return _list_devices()

	if np is None:
		print("ERROR: numpy is not installed.")
		print("Install: pip install numpy")
		if _NP_ERR is not None:
			print(f"Import error: {_NP_ERR}")
		return 2

	if sd is None and not args.no_play:
		print("ERROR: sounddevice is not installed.")
		print("Install: pip install sounddevice")
		if _SD_ERR is not None:
			print(f"Import error: {_SD_ERR}")
		return 2

	channels = max(1, int(args.channels))
	sample_rate = max(8000, int(args.sample_rate))
	seconds = max(0.1, float(args.seconds))

	print("=== SPEAKER OUTPUT PROOF ===")
	print(f"Pattern: {args.pattern}   Seconds: {seconds}   Volume: {args.volume}")
	print(f"Sample rate: {sample_rate} Hz   Channels: {channels}")

	audio, peak = _generate(
		pattern=str(args.pattern),
		seconds=float(seconds),
		sample_rate=int(sample_rate),
		channels=int(channels),
		volume=float(args.volume),
		freq=float(args.freq),
	)

	frames_total = int(audio.shape[0])
	print(f"Generated frames: {frames_total}  (expected ~{int(seconds*sample_rate)})")
	print(f"Peak abs amplitude: {peak:.3f} (float32 full-scale is 1.0)")

	if args.wav_out:
		if sf is None:
			print("WARNING: soundfile not installed; cannot write WAV.")
			print("Install: pip install soundfile")
			if _SF_ERR is not None:
				print(f"Import error: {_SF_ERR}")
		else:
			out_path = str(args.wav_out)
			sf.write(out_path, audio, samplerate=int(sample_rate), subtype="PCM_16")
			print(f"Wrote WAV: {out_path}")

	if args.no_play:
		print("\n(no-play) Not opening audio device.")
		return 0

	assert sd is not None
	dev_index = _pick_device(args.device)
	if dev_index is None:
		print("ERROR: Could not select an output device.")
		print("Try: python test_speaker.py --list-devices")
		return 2

	dev_info = sd.query_devices(dev_index)
	if int(dev_info.get("max_output_channels", 0)) <= 0:
		print(f"ERROR: Selected device {dev_index} has no output channels.")
		print("Try: python test_speaker.py --list-devices")
		return 2

	print(f"Using output device {dev_index}: {dev_info.get('name')}")
	print("Playing... (Ctrl+C to stop)")

	try:
		frames_written, elapsed = _play(audio, dev_index, sample_rate)
	except KeyboardInterrupt:
		print("\nStopped by user.")
		frames_written = 0
		elapsed = 0.0
	except Exception as e:
		print(f"ERROR: Playback failed: {e}")
		print("Common causes:")
		print("- Another app has the device open")
		print("- PulseAudio/PipeWire/ALSA routing issues")
		print("- Bluetooth output not connected / wrong default sink")
		return 1

	seconds_actual = float(elapsed)
	stats = RunStats(
		sample_rate=int(sample_rate),
		channels=int(channels),
		frames_total=int(frames_total),
		frames_written=int(frames_written),
		peak_abs=float(peak),
		seconds_target=float(seconds),
		seconds_actual=seconds_actual,
	)

	print("\n=== OUTPUT PROOF SUMMARY ===")
	print(f"Frames written: {stats.frames_written}/{stats.frames_total}")
	if stats.seconds_actual > 0:
		print(f"Elapsed: {stats.seconds_actual:.2f}s (target {stats.seconds_target:.2f}s)")
		print(f"Effective sample throughput: {stats.frames_written/stats.seconds_actual:.0f} frames/s")

	if stats.frames_written > 0:
		print("PASS: Python successfully opened an output stream and wrote audio frames.")
		print("If you heard nothing, check: system volume, mute, output routing, device selection.")
		return 0

	print("WARN: No frames written.")
	return 1


if __name__ == "__main__":
	raise SystemExit(main())