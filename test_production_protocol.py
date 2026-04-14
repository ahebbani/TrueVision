#!/usr/bin/env python3
"""Production Protocol UART Test — Raspberry Pi receiver.

Receives and validates framed packets from the ESP32 production protocol
test sketch (production_protocol_test.ino).  Reports packet counts,
corruption rate, and throughput so you can tell which baud rates are
reliable on your Pi hardware.

Usage:
    sudo python test_production_protocol.py --baud 230400 --seconds 5

Test plan (change TEST_BAUD in the sketch each time and reflash):
    1. 230400  — should work; enough for 8 kHz audio
    2. 460800  — if this works, enough for 16 kHz audio
    3. 921600  — fastest; enough for 16 kHz with headroom
Use the highest rate that reports PASS.
"""

import argparse
import os
import struct
import sys
import time

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

SYNC = bytes([0xAA, 0x55])
PKT_AUDIO = 0x01
PKT_MODE_CHANGE = 0x02
MODE_NAMES = {0x00: "AUDIO", 0x01: "FACE", 0x02: "BOTH"}


def main():
    p = argparse.ArgumentParser(description="Test TrueVision production UART protocol")
    p.add_argument("--port", default="/dev/serial0",
                   help="Serial port (default: /dev/serial0)")
    p.add_argument("--baud", type=int, default=230400,
                   help="Baud rate — must match the value in the ESP32 sketch")
    p.add_argument("--seconds", type=float, default=5.0,
                   help="How many seconds to capture (default: 5)")
    args = p.parse_args()

    # Show what the symlink resolves to
    resolved = os.path.realpath(args.port)
    if resolved != args.port:
        print(f"port={args.port} -> {resolved}  baud={args.baud}  seconds={args.seconds}")
    else:
        print(f"port={args.port}  baud={args.baud}  seconds={args.seconds}")

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
        )
    except Exception as e:
        print(f"ERROR: Could not open {args.port} at {args.baud}: {e}")
        sys.exit(1)

    audio_ok = 0
    mode_ok = 0
    corrupt = 0
    total_audio_bytes = 0
    modes_seen = []
    raw_bytes_read = 0

    deadline = time.time() + args.seconds
    buf = bytearray()

    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf.extend(chunk)
            raw_bytes_read += len(chunk)

        # Parse packets from buffer
        while len(buf) >= 6:
            idx = buf.find(SYNC)
            if idx < 0:
                buf = buf[-1:]  # keep last byte (could be start of sync)
                break
            if idx > 0:
                buf = buf[idx:]
                continue

            # Sync at position 0
            if len(buf) < 5:
                break

            pkt_type = buf[2]
            pkt_len = buf[3] | (buf[4] << 8)

            if pkt_len > 4096:
                corrupt += 1
                buf = buf[2:]
                continue

            total_frame = 5 + pkt_len + 1
            if len(buf) < total_frame:
                break

            data = bytes(buf[5 : 5 + pkt_len])
            checksum = buf[5 + pkt_len]
            expected = sum(data) & 0xFF

            if checksum != expected:
                corrupt += 1
                buf = buf[2:]
                continue

            # Valid packet
            if pkt_type == PKT_AUDIO:
                audio_ok += 1
                total_audio_bytes += pkt_len
            elif pkt_type == PKT_MODE_CHANGE:
                mode_ok += 1
                if pkt_len >= 1:
                    modes_seen.append(MODE_NAMES.get(data[0], f"0x{data[0]:02x}"))
            # else: unknown type, still valid framing

            buf = buf[total_frame:]

    ser.close()

    total_ok = audio_ok + mode_ok
    total_all = total_ok + corrupt

    print(f"\n{'=' * 55}")
    print(f"Results at {args.baud} baud over {args.seconds:.1f}s:")
    print(f"  Raw bytes received:   {raw_bytes_read}")
    print(f"  Audio packets OK:     {audio_ok}")
    print(f"  Mode packets OK:      {mode_ok}")
    print(f"  Corrupted packets:    {corrupt}")
    print(f"  Total audio data:     {total_audio_bytes} bytes")
    if args.seconds > 0:
        print(f"  Audio throughput:     {total_audio_bytes / args.seconds:.0f} bytes/sec")
    if modes_seen:
        print(f"  Modes received:       {', '.join(modes_seen)}")

    print()
    if total_ok > 0 and corrupt == 0:
        # Estimate max sustainable sample rate at this baud
        usable_bps = args.baud / 10
        # 16kHz mono 16-bit = 32000 bytes/sec + ~2% framing overhead
        can_16k = usable_bps > 33000
        can_8k = usable_bps > 16500
        if can_16k:
            print(f"  PASS  {args.baud} baud works — supports 16 kHz audio")
        elif can_8k:
            print(f"  PASS  {args.baud} baud works — supports 8 kHz audio (not 16 kHz)")
        else:
            print(f"  PASS  {args.baud} baud works — but too slow for real-time audio")
    elif total_ok > 0:
        pct = corrupt / total_all * 100
        print(f"  PARTIAL  {pct:.1f}% packet corruption — unreliable at {args.baud}")
    else:
        print(f"  FAIL  No valid packets received at {args.baud} baud")

    print()
    # Throughput reference table
    print("Baud rate reference:")
    print("  115200  ->  ~11 KB/s  (too slow for audio)")
    print("  230400  ->  ~23 KB/s  (supports 8 kHz audio)")
    print("  460800  ->  ~46 KB/s  (supports 16 kHz audio)")
    print("  921600  ->  ~92 KB/s  (supports 16 kHz with headroom)")


if __name__ == "__main__":
    main()
