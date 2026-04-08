"""ESP32 Serial Audio Receiver for Raspberry Pi.

Receives audio data from ESP32 via UART/Serial connection.
The ESP32 sends raw 16-bit PCM audio at 16 kHz mono.

Extended bidirectional protocol (truevision_main.ino):
    Frame: [SYNC(2)] [TYPE(1)] [LENGTH(2 LE)] [DATA(LENGTH)] [CHECKSUM(1)]
    - SYNC:     0xAA 0x55
    - TYPE:     packet type (see PKT_* constants below)
    - LENGTH:   number of data bytes, little-endian uint16
    - DATA:     payload bytes
    - CHECKSUM: sum(data bytes) & 0xFF

    ESP32 → Pi types:
        0x01  AUDIO_DATA   raw int16 PCM
        0x02  MODE_CHANGE  1-byte: 0x00=AUDIO, 0x01=FACE
        0x03  MARKER       0 bytes (Pi timestamps on receipt)
        0x04  DIAG_REQUEST 0 bytes

    Pi → ESP32 types:
        0x10  HEARTBEAT    1-byte: 0x00
        0x11  PI_STATUS    1+ bytes: error_code + optional ASCII
        0x12  ACK          1-byte: echoed TYPE

Usage:
    receiver = ESP32SerialAudioReceiver(
        port='/dev/serial0',
        baud_rate=921600,
        oled_missing=False,          # set True when Pi OLED is absent
        on_mode_change=my_callback,  # called with (mode_byte,)
        on_marker=my_callback,       # called with no args
        on_diag_request=my_callback, # called with no args
    )
    receiver.start()
    audio_bytes = receiver.get_last_n_seconds(5.0)
    receiver.stop()
"""
import os
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np
import soundfile as sf

try:
    import serial
except ImportError:
    serial = None  # type: ignore

# ── Packet type constants (must match truevision_main.ino) ───────────────────
# ESP32 → Pi
PKT_AUDIO        = 0x01
PKT_MODE_CHANGE  = 0x02
PKT_MARKER       = 0x03
PKT_DIAG_REQUEST = 0x04
# Pi → ESP32
PKT_HEARTBEAT    = 0x10
PKT_PI_STATUS    = 0x11
PKT_ACK          = 0x12

# PI_STATUS error codes sent in PKT_PI_STATUS payload
PI_STATUS_OK               = 0x00
PI_STATUS_CAMERA_FAIL      = 0x01
PI_STATUS_MODEL_LOAD_FAIL  = 0x02
PI_STATUS_DB_ERROR         = 0x03
PI_STATUS_SUMMARIZER_TIMEOUT = 0x04

# Operating modes (MODE_CHANGE payload values)
MODE_AUDIO = 0x00
MODE_FACE  = 0x01


def probe_esp32_uart_stream(port: str = '/dev/serial0', baud_rate: int = 921600, timeout_sec: float = 1.0) -> bool:
    """Return True if the ESP32 audio stream appears present on the UART.

    This is a best-effort probe used for auto-selecting audio source.
    It looks for the framing sync bytes 0xAA 0x55 in incoming data.
    """
    if serial is None:
        return False

    try:
        ser = serial.Serial(
            port=port,
            baudrate=int(baud_rate),
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
    except Exception:
        return False

    sync = bytes([ESP32SerialAudioReceiver.SYNC_BYTE_1, ESP32SerialAudioReceiver.SYNC_BYTE_2])
    buf = bytearray()
    deadline = time.time() + float(timeout_sec)
    try:
        while time.time() < deadline:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
                if sync in buf:
                    return True
                # keep buffer bounded
                if len(buf) > 8192:
                    buf = buf[-2048:]
    finally:
        try:
            ser.close()
        except Exception:
            pass

    return False


class ESP32SerialAudioReceiver:
    """Receives and buffers audio from ESP32 via serial connection."""

    SYNC_BYTE_1      = 0xAA
    SYNC_BYTE_2      = 0x55
    SAMPLE_RATE      = 16000  # Hz
    CHANNELS         = 1      # Mono
    BYTES_PER_SAMPLE = 2      # 16-bit

    def __init__(
        self,
        port: str = '/dev/serial0',
        baud_rate: int = 921600,
        buffer_seconds: float = 60.0,
        oled_missing: bool = False,
        on_mode_change: Optional[Callable[[int], None]] = None,
        on_marker: Optional[Callable[[], None]] = None,
        on_diag_request: Optional[Callable[[], None]] = None,
    ):
        """Initialize serial audio receiver.

        Args:
            port: Serial port device path (e.g., '/dev/serial0')
            baud_rate: UART baud rate (921600 recommended for Pi)
            buffer_seconds: Maximum seconds of audio to keep in ring buffer
            oled_missing: When True, the receiver will push PI_STATUS packets
                to the ESP32 whenever a non-OK status is set via send_pi_status().
                If False, PI_STATUS is only sent in response to DIAG_REQUEST.
            on_mode_change: Called with the new mode byte (MODE_AUDIO / MODE_FACE)
                when the ESP32 sends a PKT_MODE_CHANGE packet.
            on_marker: Called (no args) when the ESP32 sends PKT_MARKER.
            on_diag_request: Called (no args) when the ESP32 sends PKT_DIAG_REQUEST.
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed. Install with: pip install pyserial")
        
        self.port = port
        self.baud_rate = baud_rate
        self.buffer_seconds = buffer_seconds
        self.max_buffer_bytes = int(buffer_seconds * self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)

        # Callbacks (called from the receiver daemon thread)
        self.on_mode_change: Optional[Callable[[int], None]] = on_mode_change
        self.on_marker: Optional[Callable[[], None]] = on_marker
        self.on_diag_request: Optional[Callable[[], None]] = on_diag_request

        # When True, send PI_STATUS proactively (OLED absent — no other display)
        self.oled_missing: bool = oled_missing

        self._serial: Optional[serial.Serial] = None
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._write_lock = threading.Lock()   # serialise UART writes
        self._thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

        # Statistics
        self.packets_received = 0
        self.packets_corrupted = 0
        self.bytes_received = 0
    
    def start(self) -> None:
        """Start receiving audio from serial port."""
        if self._running:
            return
        
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1.0  # 1 second read timeout
            )
            print(f"ESP32 Serial Audio: Connected to {self.port} at {self.baud_rate} baud")
        except Exception as e:
            raise RuntimeError(f"Failed to open serial port {self.port}: {e}")
        
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._receiver_loop, daemon=True, name='esp32-rx')
        self._thread.start()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name='esp32-hb')
        self._hb_thread.start()
    
    def stop(self) -> None:
        """Stop receiving and close serial port."""
        if not self._running:
            return
        
        self._stop_event.set()
        self._running = False
        
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None
        
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        
        print(f"ESP32 Serial Audio: Stopped. Stats - Packets: {self.packets_received}, "
              f"Corrupted: {self.packets_corrupted}, Bytes: {self.bytes_received}")
    
    # ── Internal packet builder / sender ────────────────────────────────────

    def _build_packet(self, pkt_type: int, data: bytes) -> bytes:
        """Build a framed packet with the extended bidirectional format."""
        data_len = len(data)
        header = bytes([
            self.SYNC_BYTE_1,
            self.SYNC_BYTE_2,
            pkt_type,
            data_len & 0xFF,
            (data_len >> 8) & 0xFF,
        ])
        checksum = sum(data) & 0xFF
        return header + data + bytes([checksum])

    def _send_raw(self, packet: bytes) -> None:
        """Thread-safe write to the serial port."""
        if self._serial is None or not self._running:
            return
        with self._write_lock:
            try:
                self._serial.write(packet)
            except Exception as e:
                print(f"ESP32 Serial Audio: TX error: {e}")

    def _send_ack(self, acked_type: int) -> None:
        self._send_raw(self._build_packet(PKT_ACK, bytes([acked_type])))

    # ── Heartbeat sender ─────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Send PKT_HEARTBEAT to the ESP32 every 3 seconds."""
        hb_packet = self._build_packet(PKT_HEARTBEAT, bytes([0x00]))
        while not self._stop_event.is_set():
            self._send_raw(hb_packet)
            self._stop_event.wait(timeout=3.0)

    # ── Public method: push Pi status to ESP32 ────────────────────────────────

    def send_pi_status(self, error_code: int, message: str = '', force: bool = False) -> None:
        """Send a PKT_PI_STATUS packet to the ESP32.

        Args:
            error_code: One of the PI_STATUS_* constants from this module.
            message:    Optional ASCII description (truncated to 64 characters).
            force:      If True, send even when oled_missing is False.  The
                        receiver always sends in response to DIAG_REQUEST
                        regardless of this flag; call this directly for
                        proactive status push.
        """
        if not self.oled_missing and not force:
            return
        msg_bytes = message[:64].encode('ascii', errors='replace')
        payload = bytes([error_code]) + msg_bytes
        self._send_raw(self._build_packet(PKT_PI_STATUS, payload))

    # ── Receiver loop ─────────────────────────────────────────────────────────

    def _receiver_loop(self) -> None:
        """Background thread that continuously reads from serial port."""
        print("ESP32 Serial Audio: Receiver thread started")

        while not self._stop_event.is_set() and self._serial is not None:
            try:
                # Locate the sync pair 0xAA 0x55
                if not self._find_sync():
                    continue

                # Read TYPE byte (1 byte)
                type_byte = self._serial.read(1)
                if len(type_byte) != 1:
                    continue
                pkt_type = type_byte[0]

                # Read packet length (2 bytes, little-endian)
                length_bytes = self._serial.read(2)
                if len(length_bytes) != 2:
                    continue
                length = struct.unpack('<H', length_bytes)[0]

                # Sanity check on length
                if length > 4096:
                    self.packets_corrupted += 1
                    continue

                # Read payload
                data = self._serial.read(length) if length > 0 else b''
                if len(data) != length:
                    continue

                # Read and verify checksum
                cs_byte = self._serial.read(1)
                if len(cs_byte) != 1:
                    continue
                if (sum(data) & 0xFF) != cs_byte[0]:
                    self.packets_corrupted += 1
                    continue

                # Dispatch by type
                if pkt_type == PKT_AUDIO:
                    # Audio data — append to ring buffer
                    with self._buffer_lock:
                        self._buffer.extend(data)
                        if len(self._buffer) > self.max_buffer_bytes:
                            trim = len(self._buffer) - self.max_buffer_bytes
                            self._buffer = self._buffer[trim:]
                    self.packets_received += 1
                    self.bytes_received += length

                elif pkt_type == PKT_MODE_CHANGE:
                    if length >= 1 and self.on_mode_change is not None:
                        try:
                            self.on_mode_change(data[0])
                        except Exception as cb_err:
                            print(f"ESP32 Serial Audio: on_mode_change error: {cb_err}")

                elif pkt_type == PKT_MARKER:
                    if self.on_marker is not None:
                        try:
                            self.on_marker()
                        except Exception as cb_err:
                            print(f"ESP32 Serial Audio: on_marker error: {cb_err}")

                elif pkt_type == PKT_DIAG_REQUEST:
                    # Always respond with current status + ACK regardless of oled_missing
                    self.send_pi_status(PI_STATUS_OK, force=True)
                    self._send_ack(PKT_DIAG_REQUEST)
                    if self.on_diag_request is not None:
                        try:
                            self.on_diag_request()
                        except Exception as cb_err:
                            print(f"ESP32 Serial Audio: on_diag_request error: {cb_err}")

                # Unknown types are silently ignored (forward-compatible)

            except Exception as e:
                if not self._stop_event.is_set():
                    print(f"ESP32 Serial Audio: Error in receiver loop: {e}")
                time.sleep(0.1)

        print("ESP32 Serial Audio: Receiver thread stopped")
    
    def _find_sync(self) -> bool:
        """Search for sync bytes in serial stream."""
        if self._serial is None:
            return False
        
        # Read bytes one at a time until we find sync pattern
        max_attempts = 100
        for _ in range(max_attempts):
            if self._stop_event.is_set():
                return False
            
            byte1 = self._serial.read(1)
            if len(byte1) != 1:
                continue
            
            if byte1[0] == self.SYNC_BYTE_1:
                byte2 = self._serial.read(1)
                if len(byte2) == 1 and byte2[0] == self.SYNC_BYTE_2:
                    return True
        
        return False
    
    def get_last_n_seconds(self, seconds: float) -> bytes:
        """Get the last N seconds of audio from buffer.
        
        Args:
            seconds: Number of seconds of audio to retrieve
            
        Returns:
            Raw PCM audio bytes (16-bit little-endian samples)
        """
        bytes_needed = int(seconds * self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)
        
        with self._buffer_lock:
            if len(self._buffer) < bytes_needed:
                # Return all available data
                return bytes(self._buffer)
            else:
                # Return last N seconds
                return bytes(self._buffer[-bytes_needed:])
    
    def get_all_audio(self) -> bytes:
        """Get all audio currently in buffer."""
        with self._buffer_lock:
            return bytes(self._buffer)
    
    def clear_buffer(self) -> None:
        """Clear all audio from buffer."""
        with self._buffer_lock:
            self._buffer.clear()
    
    def get_buffer_duration(self) -> float:
        """Get duration of audio currently in buffer (seconds)."""
        with self._buffer_lock:
            num_samples = len(self._buffer) // self.BYTES_PER_SAMPLE
            return num_samples / self.SAMPLE_RATE
    
    def is_receiving(self) -> bool:
        """Check if receiver is actively running."""
        return self._running
    
    def write_to_wav(self, output_path: str, seconds: Optional[float] = None) -> bool:
        """Write buffered audio to a WAV file.
        
        Args:
            output_path: Path to output WAV file
            seconds: If specified, only write last N seconds. If None, write all.
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if seconds is not None:
                audio_bytes = self.get_last_n_seconds(seconds)
            else:
                audio_bytes = self.get_all_audio()
            
            if len(audio_bytes) == 0:
                return False
            
            # Convert bytes to numpy array (16-bit signed integers)
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
            
            # Write to WAV file
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            sf.write(output_path, audio_array, self.SAMPLE_RATE, subtype='PCM_16')
            
            return True
        except Exception as e:
            print(f"ESP32 Serial Audio: Error writing WAV file: {e}")
            return False


class ESP32SerialRecorder:
    """Drop-in replacement for Recorder class that uses ESP32 serial audio.
    
    Compatible with existing transcription.py Recorder interface.
    """
    
    def __init__(self, serial_receiver: ESP32SerialAudioReceiver, 
                 sample_rate: int = 16000, channels: int = 1):
        """Initialize recorder with existing serial receiver.
        
        Args:
            serial_receiver: ESP32SerialAudioReceiver instance (must be started)
            sample_rate: Audio sample rate (must match ESP32)
            channels: Number of audio channels (must match ESP32)
        """
        self.receiver = serial_receiver
        self.sample_rate = sample_rate
        self.channels = channels
        self.audio_path: Optional[str] = None
        self._start_time: Optional[float] = None
        self._recording = False
    
    def start(self, directory: str, filename_prefix: str = "meeting") -> str:
        """Start a recording session."""
        from datetime import datetime
        
        os.makedirs(directory, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.audio_path = os.path.join(directory, f"{filename_prefix}_{ts}.wav")
        self._start_time = time.time()
        self._recording = True
        
        # Clear buffer to start fresh
        self.receiver.clear_buffer()
        
        return self.audio_path
    
    def stop(self) -> Optional[str]:
        """Stop recording and write audio to file."""
        if not self._recording or self.audio_path is None:
            return None
        
        self._recording = False
        
        # Calculate recording duration
        duration = time.time() - self._start_time if self._start_time else None
        
        # Write all buffered audio to file
        success = self.receiver.write_to_wav(self.audio_path, seconds=duration)
        
        path = self.audio_path if success else None
        self.audio_path = None
        self._start_time = None
        
        return path
