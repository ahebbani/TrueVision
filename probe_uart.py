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
from typing import Optional, Sequence, Tuple

try:
    import serial  # type: ignore
except Exception as e:  # pragma: no cover
    serial = None
    _serial_import_error = e


def _probe_once(port: str, baud: int, seconds: float, show: int) -> Tuple[int, bool, bytes]:
    ser = serial.Serial(
        port=port,
        baudrate=int(baud),
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.05,
    )

    sync = b"\xAA\x55"
    started = time.time()
    buf = bytearray()
    total = 0

    try:
        while time.time() - started < float(seconds):
            chunk = ser.read(4096)
            if chunk:
                total += len(chunk)
                if len(buf) < max(2048, show):
                    buf.extend(chunk)
                else:
                    # keep some tail for sync detection
                    buf = buf[-1024:] + bytearray(chunk)
    finally:
        try:
            ser.close()
        except Exception:
            pass

    preview = bytes(buf[:show]) if show > 0 else b""
    return total, (sync in buf), preview


def main() -> int:
    p = argparse.ArgumentParser(description="Probe raw bytes on a serial port")
    p.add_argument("--port", default="/dev/serial0")
    p.add_argument(
        "--baud",
        default="921600",
        help="Baud rate (e.g. 921600) or 'auto' to try common rates",
    )
    p.add_argument(
        "--bauds",
        nargs="+",
        type=int,
        default=[115200, 230400, 460800, 921600],
        help="Baud rates to try when --baud auto",
    )
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--show", type=int, default=64, help="Show first N bytes in hex (default: 64)")
    args = p.parse_args()

    if serial is None:
        print(f"ERROR: pyserial not available ({_serial_import_error}).")
        print("Install with: pip install pyserial")
        return 2

    baud_arg = str(args.baud).strip().lower()

    def _print_result(baud: int, total: int, sync_seen: bool, preview: bytes) -> None:
        print(f"port={args.port} baud={baud} seconds={args.seconds}")
        print(f"bytes_read={total}")
        print(f"sync_AA55_seen={sync_seen}")
        if args.show > 0:
            hex_preview = " ".join(f"{b:02X}" for b in preview)
            print(f"first_{len(preview)}_bytes_hex={hex_preview}")

    if baud_arg == "auto":
        any_bytes = False
        for b in list(args.bauds):
            try:
                total, sync_seen, preview = _probe_once(args.port, int(b), float(args.seconds), int(args.show))
            except Exception as e:
                print(f"port={args.port} baud={b} seconds={args.seconds}")
                print(f"ERROR: {e}")
                print("---")
                continue

            _print_result(int(b), total, sync_seen, preview)
            print("---")
            any_bytes = any_bytes or (total > 0)
            if sync_seen:
                return 0
        return 1 if any_bytes else 4

    try:
        baud = int(baud_arg)
    except Exception:
        print("ERROR: --baud must be an integer or 'auto'.")
        return 2

    try:
        total, sync_seen, preview = _probe_once(args.port, baud, float(args.seconds), int(args.show))
    except Exception as e:
        print(f"ERROR: Could not open/probe {args.port} at {baud}: {e}")
        return 3

    _print_result(baud, total, sync_seen, preview)

    # Exit code convention:
    # 0 = sync detected
    # 1 = bytes arrived but no sync detected
    # 4 = no bytes at all
    if total == 0:
        return 4
    return 0 if sync_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
