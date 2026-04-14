#!/usr/bin/env python3
"""Test that the Raspberry Pi receives ESP32 MODE_CHANGE packets.

Listens on the serial port using the production protocol and prints
every packet type received.  The simplified ESP32 firmware streams
continuously and sends MODE_CHANGE on button press/release — no
heartbeats or commands are sent to the ESP32.

Usage:
    sudo python test_mode_switch.py
    sudo python test_mode_switch.py --port /dev/serial0 --baud 921600 --seconds 30
"""

import argparse
import struct
import sys
import time

try:
    import serial
    from serial import SerialException
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

SYNC = bytes([0xAA, 0x55])
PKT_NAMES = {
    0x01: "AUDIO",
    0x02: "MODE_CHANGE",
}
MODE_NAMES = {0x00: "AUDIO", 0x01: "FACE"}


def main():
    p = argparse.ArgumentParser(description="Listen for ESP32 MODE_CHANGE packets")
    p.add_argument("--port", default="/dev/serial0")
    p.add_argument("--baud", type=int, default=921600)
    p.add_argument("--seconds", type=float, default=30.0,
                   help="How long to listen (default: 30)")
    args = p.parse_args()

    print(f"Listening on {args.port} at {args.baud} baud for {args.seconds:.0f}s...")
    print("Press/release the mode button now. You should see MODE_CHANGE lines.\n")

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
        print(f"ERROR: Could not open {args.port}: {e}")
        sys.exit(1)

    audio_count = 0
    mode_count = 0
    other_count = 0
    corrupt_count = 0
    idle_errors = 0
    buf = bytearray()
    deadline = time.time() + args.seconds
    last_status = time.time()

    while time.time() < deadline:
        try:
            chunk = ser.read(4096)
        except SerialException as se:
            if "returned no data" in str(se):
                idle_errors += 1
                time.sleep(0.05)
                continue
            raise

        if chunk:
            buf.extend(chunk)

        # Parse packets
        while len(buf) >= 6:
            idx = buf.find(SYNC)
            if idx < 0:
                buf = buf[-1:]
                break
            if idx > 0:
                buf = buf[idx:]
                continue

            if len(buf) < 5:
                break

            pkt_type = buf[2]
            pkt_len = buf[3] | (buf[4] << 8)

            if pkt_len > 4096:
                corrupt_count += 1
                buf = buf[2:]
                continue

            total_frame = 5 + pkt_len + 1
            if len(buf) < total_frame:
                break

            data = bytes(buf[5 : 5 + pkt_len])
            checksum = buf[5 + pkt_len]
            expected = sum(data) & 0xFF

            if checksum != expected:
                corrupt_count += 1
                buf = buf[2:]
                continue

            # Valid packet
            name = PKT_NAMES.get(pkt_type, f"UNKNOWN(0x{pkt_type:02x})")

            if pkt_type == 0x02:  # MODE_CHANGE
                mode_count += 1
                mode_str = MODE_NAMES.get(data[0], f"0x{data[0]:02x}") if pkt_len >= 1 else "?"
                elapsed = time.time() - (deadline - args.seconds)
                print(f"  [{elapsed:6.1f}s] *** MODE_CHANGE → {mode_str} ***")
            elif pkt_type == 0x01:  # AUDIO
                audio_count += 1
            else:
                other_count += 1
                elapsed = time.time() - (deadline - args.seconds)
                print(f"  [{elapsed:6.1f}s] {name} (len={pkt_len})")

            buf = buf[total_frame:]

        # Periodic status line every 5 seconds
        now = time.time()
        if now - last_status >= 5.0:
            last_status = now
            elapsed = now - (deadline - args.seconds)
            print(f"  [{elapsed:6.1f}s] ... audio={audio_count} mode={mode_count} "
                  f"corrupt={corrupt_count} idle_err={idle_errors}")

    ser.close()

    print(f"\n{'=' * 50}")
    print(f"Summary ({args.seconds:.0f}s on {args.port} at {args.baud}):")
    print(f"  Audio packets:      {audio_count}")
    print(f"  Mode changes:       {mode_count}")
    print(f"  Other packets:      {other_count}")
    print(f"  Corrupted:          {corrupt_count}")
    print(f"  Idle-line errors:   {idle_errors}")

    if mode_count > 0:
        print(f"\n  PASS — {mode_count} MODE_CHANGE packet(s) received")
    elif audio_count > 0:
        print(f"\n  PARTIAL — Audio packets received but NO mode changes.")
        print(f"  The ESP32 is transmitting but the switch may not be triggering MODE_CHANGE.")
    else:
        print(f"\n  FAIL — No valid packets received at all.")


if __name__ == "__main__":
    main()
