#!/usr/bin/env python3
"""Raw UART probe for ESP32 audio streaming.

Use this when the packet receiver shows 0 packets and you need to answer:
- Are *any* bytes arriving on the Pi UART?
- Do we ever see the expected sync bytes 0xAA 0x55?

Examples:
  python probe_uart.py --port /dev/serial0 --baud 921600 --seconds 2
  python probe_uart.py --port /dev/ttyAMA0 --baud 460800 --seconds 3
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import serial  # type: ignore
except Exception as e:  # pragma: no cover
    serial = None
    _serial_import_error = e


def main() -> int:
    p = argparse.ArgumentParser(description="Probe raw bytes on a serial port")
    p.add_argument("--port", default="/dev/serial0")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--show", type=int, default=64, help="Show first N bytes in hex (default: 64)")
    args = p.parse_args()

    if serial is None:
        print(f"ERROR: pyserial not available ({_serial_import_error}).")
        print("Install with: pip install pyserial")
        return 2

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=int(args.baud),
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
    except Exception as e:
        print(f"ERROR: Could not open {args.port}: {e}")
        return 3

    sync = b"\xAA\x55"
    started = time.time()
    buf = bytearray()
    total = 0

    try:
        while time.time() - started < float(args.seconds):
            chunk = ser.read(4096)
            if chunk:
                total += len(chunk)
                if len(buf) < max(2048, args.show):
                    buf.extend(chunk)
                else:
                    # keep some tail for sync detection
                    buf = buf[-1024:] + bytearray(chunk)
    finally:
        try:
            ser.close()
        except Exception:
            pass

    sync_seen = (sync in buf)

    print(f"port={args.port} baud={args.baud} seconds={args.seconds}")
    print(f"bytes_read={total}")
    print(f"sync_AA55_seen={sync_seen}")

    if args.show > 0:
        preview = bytes(buf[: args.show])
        hex_preview = " ".join(f"{b:02X}" for b in preview)
        print(f"first_{len(preview)}_bytes_hex={hex_preview}")

    # Exit code convention:
    # 0 = sync detected
    # 1 = bytes arrived but no sync detected
    # 4 = no bytes at all
    if total == 0:
        return 4
    return 0 if sync_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
