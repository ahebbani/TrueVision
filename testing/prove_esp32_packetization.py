#!/usr/bin/env python3
"""Prove ESP32 UART framing is correct for the current TrueVision firmware.

This script is intentionally chatty. It validates the packet stream produced by
esp32_firmware/truevision_main.ino and explains whether the Pi is receiving:

- valid audio packets
- only control packets (link works, audio currently suppressed)
- raw bytes that never decode into valid packets (usually a baud mismatch)

Protocol (matches truevision_main.ino and audio_analysis/esp32_serial_audio.py):
    [SYNC(0xAA 0x55)] [TYPE(1)] [LENGTH(2 bytes LE)] [DATA] [CHECKSUM(1)]

Validation performed:
- Finds SYNC pattern in raw byte stream (proves packet boundaries exist)
- Parses TYPE to distinguish audio frames from control traffic
- Parses LENGTH and enforces sane bounds
- Reads exactly LENGTH payload bytes
- Verifies CHECKSUM == (sum(payload) & 0xFF)
- Optionally prints basic audio sanity stats (peak/RMS-ish)

Examples:
  python prove_esp32_packetization.py --list-ports
  python prove_esp32_packetization.py --port /dev/cu.usbserial-XXXX --baud 921600 --seconds 10
  python prove_esp32_packetization.py --port /dev/serial0 --baud 921600 --max-packets 200 --verbose
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception as e:  # pragma: no cover
    serial = None  # type: ignore
    list_ports = None  # type: ignore
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


SYNC = b"\xAA\x55"
MAX_LEN = 4096

PKT_AUDIO = 0x01
PKT_MODE_CHANGE = 0x02
PKT_MARKER = 0x03
PKT_DIAG_REQUEST = 0x04
PKT_HEARTBEAT = 0x10
PKT_PI_STATUS = 0x11
PKT_ACK = 0x12

PKT_NAMES = {
    PKT_AUDIO: "AUDIO",
    PKT_MODE_CHANGE: "MODE_CHANGE",
    PKT_MARKER: "MARKER",
    PKT_DIAG_REQUEST: "DIAG_REQUEST",
    PKT_HEARTBEAT: "HEARTBEAT",
    PKT_PI_STATUS: "PI_STATUS",
    PKT_ACK: "ACK",
}


@dataclass
class Packet:
    packet_type: int
    length: int
    payload: bytes
    expected_checksum: int
    actual_checksum: int
    checksum_ok: bool


def _packet_name(packet_type: int) -> str:
    return PKT_NAMES.get(packet_type, f"0x{packet_type:02X}")


def _print_ports() -> int:
    if list_ports is None:
        print("pyserial is not installed; cannot list ports.")
        return 2

    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return 1

    print("Available serial ports:")
    for p in ports:
        desc = p.description or "(no description)"
        manuf = p.manufacturer or ""
        hwid = p.hwid or ""
        extras = " | ".join(x for x in [manuf, hwid] if x)
        if extras:
            print(f"  {p.device}  -  {desc}  ({extras})")
        else:
            print(f"  {p.device}  -  {desc}")

    return 0


def _choose_default_port() -> Optional[str]:
    if list_ports is None:
        return None

    ports = list(list_ports.comports())
    if not ports:
        return None

    # Prefer USB serial adapters.
    preferred = []
    for p in ports:
        hay = f"{p.device} {p.description or ''} {p.manufacturer or ''} {p.hwid or ''}".lower()
        score = 0
        if "usb" in hay:
            score += 5
        if "cp210" in hay or "silabs" in hay:
            score += 3
        if "ch34" in hay or "wch" in hay:
            score += 3
        if "uart" in hay:
            score += 1
        preferred.append((score, p.device))

    preferred.sort(reverse=True)
    return preferred[0][1]


def _audio_stats(payload: bytes) -> str:
    # Payload is 16-bit little-endian PCM.
    if len(payload) < 2:
        return "audio: n/a"

    sample_count = len(payload) // 2

    # Fast path without numpy: unpack a small prefix for printing, and compute
    # a coarse peak over the whole payload by iterating int16s.
    peak = 0
    abs_sum = 0

    # Iterate as signed 16-bit little-endian.
    # Using memoryview avoids copies.
    mv = memoryview(payload)
    for i in range(0, sample_count * 2, 2):
        (s,) = struct.unpack_from('<h', mv, i)
        a = -s if s < 0 else s
        abs_sum += a
        if a > peak:
            peak = a

    mean_abs = abs_sum / max(1, sample_count)
    return f"samples={sample_count} peak={peak} mean_abs={mean_abs:.0f}"


def _format_sample_preview(payload: bytes, max_samples: int = 8) -> str:
    if len(payload) < 2:
        return "[]"

    n = min(max_samples, len(payload) // 2)
    mv = memoryview(payload)
    vals = [struct.unpack_from('<h', mv, i * 2)[0] for i in range(n)]
    return "[" + ", ".join(str(v) for v in vals) + "]"


def _extract_one_packet(buf: bytearray) -> Optional[Packet]:
    """Try to parse a single packet from the front of buf.

    On success: removes the packet bytes from buf and returns a Packet.
    On failure/incomplete: returns None (buf may be trimmed to resync).
    """

    # Need at least sync + type + len + checksum minimal.
    if len(buf) < 2 + 1 + 2 + 1:
        return None

    # Sync must be at start; otherwise, resync to the next occurrence.
    if not (buf[0] == 0xAA and buf[1] == 0x55):
        idx = buf.find(SYNC)
        if idx == -1:
            # Keep last byte in case it's 0xAA (start of sync).
            keep = 1 if buf and buf[-1] == 0xAA else 0
            if keep:
                del buf[:-keep]
            else:
                buf.clear()
            return None
        if idx > 0:
            del buf[:idx]
        if len(buf) < 2 + 1 + 2 + 1:
            return None

    packet_type = buf[2]
    length = struct.unpack_from('<H', buf, 3)[0]

    if length > MAX_LEN:
        # Bad length: drop first sync byte and resync.
        del buf[0:1]
        return None

    total = 2 + 1 + 2 + length + 1
    if len(buf) < total:
        return None

    payload = bytes(buf[5 : 5 + length])
    expected = buf[5 + length]
    actual = sum(payload) & 0xFF
    ok = expected == actual

    del buf[:total]

    return Packet(
        packet_type=packet_type,
        length=length,
        payload=payload,
        expected_checksum=expected,
        actual_checksum=actual,
        checksum_ok=ok,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Validate ESP32 UART audio packet framing")
    p.add_argument("--list-ports", action="store_true", help="List serial ports and exit")
    p.add_argument("--port", default=None, help="Serial port (e.g., /dev/cu.usbserial-..., /dev/serial0)")
    p.add_argument("--baud", type=int, default=921600, help="Baud rate (default: 921600)")
    p.add_argument("--seconds", type=float, default=8.0, help="How long to run (default: 8s)")
    p.add_argument("--max-packets", type=int, default=0, help="Stop after N valid packets (0 = ignore)")
    p.add_argument("--print-every", type=int, default=25, help="Print a summary every N valid packets")
    p.add_argument("--verbose", action="store_true", help="Print every packet details")
    p.add_argument("--show-samples", action="store_true", help="Print first few PCM samples per packet")
    p.add_argument(
        "--no-dtr-rts",
        action="store_true",
        help="Force DTR/RTS low after open (helps avoid ESP32 auto-reset/boot-hold)",
    )
    args = p.parse_args()

    if args.list_ports:
        return _print_ports()

    if serial is None:
        print("ERROR: pyserial is not installed.")
        print("Install it with: pip install pyserial")
        if _IMPORT_ERROR is not None:
            print(f"Import error: {_IMPORT_ERROR}")
        return 2

    port = args.port or _choose_default_port()
    if not port:
        print("ERROR: No serial port provided and none auto-detected.")
        print("Try: python prove_esp32_packetization.py --list-ports")
        return 2

    print(f"Opening {port} @ {args.baud}...")
    try:
        ser = serial.Serial(
            port=port,
            baudrate=int(args.baud),
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
    except Exception as e:
        print(f"ERROR: Failed to open {port}: {e}")
        return 2

    # Some ESP32 boards auto-reset or get held in bootloader depending on DTR/RTS.
    # For a "pure streaming" UART link, forcing these low is often safest.
    if args.no_dtr_rts:
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass

    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    buf = bytearray()
    good = 0
    control = 0
    bad_checksum = 0
    bad_length = 0
    resyncs = 0
    raw_bytes = 0

    # Track last sync finding for proof.
    last_sync_seen_at = time.time()
    start = time.time()
    deadline = start + float(args.seconds)

    print("Reading... (Ctrl+C to stop)")
    print("Proof target: see VALID packets with checksum OK and stable lengths.")

    try:
        while True:
            now = time.time()
            if now >= deadline:
                break

            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
                raw_bytes += len(chunk)

            # Bound buffer so we don't grow forever if we never find sync.
            if len(buf) > 65536:
                del buf[:-8192]

            # Parse as many packets as possible.
            made_progress = True
            while made_progress:
                made_progress = False

                # Track resync attempts: if sync isn't at front, _extract_one_packet will trim.
                if len(buf) >= 2 and not (buf[0] == 0xAA and buf[1] == 0x55):
                    if SYNC in buf:
                        resyncs += 1

                pkt = _extract_one_packet(buf)
                if pkt is None:
                    continue

                made_progress = True

                if not pkt.checksum_ok:
                    bad_checksum += 1
                    # Keep going; checksum failures are evidence too.
                    if args.verbose:
                        print(
                            f"BAD  len={pkt.length} checksum exp=0x{pkt.expected_checksum:02X} got=0x{pkt.actual_checksum:02X}"
                        )
                    continue

                last_sync_seen_at = now

                if pkt.packet_type != PKT_AUDIO:
                    control += 1
                    if args.verbose or control <= 3:
                        print(
                            f"CTRL #{control:05d} type={_packet_name(pkt.packet_type)} len={pkt.length} checksum=0x{pkt.actual_checksum:02X}"
                        )
                    continue

                # Heuristic: treat non-even lengths as bad length for PCM16.
                if pkt.length % 2 != 0:
                    bad_length += 1
                    if args.verbose:
                        print(f"BAD  type=AUDIO len={pkt.length} (not divisible by 2 for PCM16)")
                    continue

                good += 1

                if args.verbose or good <= 5:
                    stats = _audio_stats(pkt.payload)
                    line = f"OK   #{good:05d} type=AUDIO len={pkt.length:4d} checksum=0x{pkt.actual_checksum:02X} {stats}"
                    if args.show_samples:
                        line += f" samples0={_format_sample_preview(pkt.payload)}"
                    print(line)

                if (not args.verbose) and args.print_every > 0 and good % int(args.print_every) == 0:
                    elapsed = now - start
                    rate = good / elapsed if elapsed > 0 else 0.0
                    print(
                        f"SUMMARY t={elapsed:5.1f}s audio={good} control={control} bad_checksum={bad_checksum} bad_len={bad_length} resyncs~={resyncs} audio_pkts/s={rate:.1f}"
                    )

                if args.max_packets and good >= int(args.max_packets):
                    deadline = now
                    break

            # If we haven't seen sync in a while, print a hint (proves absence).
            if (now - last_sync_seen_at) > 1.0 and good == 0:
                last_sync_seen_at = now
                elapsed = now - start
                rate = raw_bytes / elapsed if elapsed > 0 else 0.0
                print(
                    f"(no valid packets yet) raw_bytes={raw_bytes} ({rate:.0f} B/s) waiting for SYNC 0xAA 0x55..."
                )

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        try:
            ser.close()
        except Exception:
            pass

    elapsed = time.time() - start
    print("\n=== PACKETIZATION PROOF SUMMARY ===")
    print(f"Port: {port}  Baud: {args.baud}")
    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Raw bytes received: {raw_bytes}")
    print(f"Valid audio packets (checksum OK): {good}")
    print(f"Valid control packets (checksum OK): {control}")
    print(f"Checksum failures: {bad_checksum}")
    print(f"Bad audio length (PCM16 divisibility): {bad_length}")
    print(f"Resync events (saw SYNC mid-stream): ~{resyncs}")
    if good > 0 and elapsed > 0:
        print(f"Packet rate: {good/elapsed:.2f} packets/s")
        # Typical packet length from firmware: BUFFER_SIZE=512 samples => 1024 bytes.
        print("Expected audio payload length from firmware default: 1024 bytes (512 samples)")

    if good == 0:
        print("\nNo valid audio packets received.")
        if control > 0:
            print("\nControl traffic is present, so UART framing works but audio streaming is currently disabled.")
            print("This usually means the ESP32 is in FACE mode or the I2S capture path is not producing audio packets.")
            return 1
        if raw_bytes > 0:
            print("\nWe DID receive bytes, but they never decoded into valid framed packets.")
            print("This usually means one of:")
            print("- Baud rate mismatch (you'll read garbage bytes)")
            print("- Another sketch is printing text/logs instead of binary packets")
            print("- The ESP32 stream is corrupted on the wire")
            print("\nTry:")
            print("- Flash esp32_firmware/truevision_main.ino")
            print("- Ensure Serial Monitor is closed (port not busy)")
            print("- Re-run with --baud 921600 (firmware default)")
            print("- Optional sanity: set --baud 115200 and see if you get readable boot logs")
        print("Quick checks:")
        print("- Confirm ESP32 is running truevision_main.ino")
        print("- On macOS, pick the correct /dev/cu.* port: --list-ports")
        print("- Verify baud matches firmware (default 921600)")
        return 1

    if bad_checksum == 0 and bad_length == 0:
        print("\nPASS: Stream is consistently framed + checksummed.")
        return 0

    print("\nWARN: Some packets failed validation; wiring/baud/noise may be an issue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
