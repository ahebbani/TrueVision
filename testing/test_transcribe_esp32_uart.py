#!/usr/bin/env python3
"""Record audio from ESP32 over UART and transcribe it.

This is the end-to-end validation:
ESP32 (I2S mic) -> UART packets -> Raspberry Pi -> WAV -> faster-whisper transcription.

Usage:
  python transcribe_esp32_uart.py --seconds 8
  python transcribe_esp32_uart.py --port /dev/serial0 --baud 921600 --seconds 10 --model tiny

Notes:
- Requires: pyserial, soundfile, numpy (already in requirements.txt)
- Requires transcription: faster-whisper (install via requirements-faster-whisper.txt)
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_analysis.esp32_serial_audio import ESP32SerialAudioReceiver


def main() -> int:
    p = argparse.ArgumentParser(description="Record from ESP32 UART and transcribe")
    p.add_argument("--port", default="/dev/serial0")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--out", default=None, help="Output wav path (default: ./esp32_uart_<ts>.wav)")
    p.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "tiny"))
    p.add_argument("--device", default=os.environ.get("WHISPER_DEVICE", "cpu"))
    p.add_argument("--compute-type", default=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"))
    args = p.parse_args()

    # Import here so you can still run the receive test without faster-whisper installed.
    try:
        from audio_analysis.transcription import Transcriber
    except Exception as e:
        print(f"ERROR: Could not import Transcriber ({e}).")
        print("If you haven't installed faster-whisper yet, run:")
        print("\n```bash\npip install -r requirements-faster-whisper.txt\n```\n")
        return 2

    receiver = ESP32SerialAudioReceiver(port=args.port, baud_rate=args.baud, buffer_seconds=max(30.0, args.seconds + 5.0))
    receiver.start()

    print(f"Recording {args.seconds:.1f}s from {args.port} @ {args.baud}...")
    receiver.clear_buffer()
    start = time.time()
    last_packets = 0

    while time.time() - start < args.seconds:
        time.sleep(0.5)
        pk = receiver.packets_received
        print(f"  packets={pk} (+{pk - last_packets})  corrupted={receiver.packets_corrupted}  buffer={receiver.get_buffer_duration():.1f}s")
        last_packets = pk

    if args.out is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.out = f"esp32_uart_{ts}.wav"

    ok = receiver.write_to_wav(args.out, seconds=args.seconds)
    receiver.stop()

    if not ok:
        print("ERROR: No audio captured (WAV not written).")
        return 3

    print(f"Wrote WAV: {args.out}")

    transcriber = Transcriber(model_size=args.model, device=args.device, compute_type=args.compute_type)
    print("Transcribing...")
    text = transcriber.transcribe(args.out)
    print("\n--- TRANSCRIPT ---")
    print(text.strip() or "(empty)")
    print("------------------\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
