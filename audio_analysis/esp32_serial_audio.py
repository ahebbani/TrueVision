"""ESP32 Serial Audio Receiver for Raspberry Pi.

Receives audio data from ESP32 via UART/Serial connection.
The ESP32 sends raw 16-bit PCM audio at 16kHz mono.

Protocol:
    Simple framing: [SYNC(2 bytes)] [LENGTH(2 bytes)] [AUDIO_DATA] [CHECKSUM(1 byte)]
    - SYNC: 0xAA 0x55
    - LENGTH: Number of audio bytes (little-endian uint16)
    - AUDIO_DATA: Raw 16-bit PCM samples
    - CHECKSUM: Simple sum of all audio bytes & 0xFF

Usage:
    receiver = ESP32SerialAudioReceiver(port='/dev/serial0', baud_rate=921600)
    receiver.start()
    
    # Later, get audio for transcription
    audio_bytes = receiver.get_last_n_seconds(5.0)
    # Write to WAV file and transcribe...
    
    receiver.stop()
"""
import os
import struct
import threading
import time
from typing import Optional

import numpy as np
import soundfile as sf

try:
    import serial
except ImportError:
    serial = None  # type: ignore


class ESP32SerialAudioReceiver:
    """Receives and buffers audio from ESP32 via serial connection."""
    
    SYNC_BYTE_1 = 0xAA
    SYNC_BYTE_2 = 0x55
    SAMPLE_RATE = 16000  # Hz
    CHANNELS = 1  # Mono
    BYTES_PER_SAMPLE = 2  # 16-bit
    
    def __init__(self, port: str = '/dev/serial0', baud_rate: int = 921600, 
                 buffer_seconds: float = 60.0):
        """Initialize serial audio receiver.
        
        Args:
            port: Serial port device path (e.g., '/dev/serial0' or '/dev/ttyAMA0')
            baud_rate: UART baud rate (921600 recommended for Pi)
            buffer_seconds: Maximum seconds of audio to keep in buffer
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed. Install with: pip install pyserial")
        
        self.port = port
        self.baud_rate = baud_rate
        self.buffer_seconds = buffer_seconds
        self.max_buffer_bytes = int(buffer_seconds * self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)
        
        self._serial: Optional[serial.Serial] = None
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
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
        self._thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self._thread.start()
    
    def stop(self) -> None:
        """Stop receiving and close serial port."""
        if not self._running:
            return
        
        self._stop_event.set()
        self._running = False
        
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        
        print(f"ESP32 Serial Audio: Stopped. Stats - Packets: {self.packets_received}, "
              f"Corrupted: {self.packets_corrupted}, Bytes: {self.bytes_received}")
    
    def _receiver_loop(self) -> None:
        """Background thread that continuously reads from serial port."""
        print("ESP32 Serial Audio: Receiver thread started")
        
        while not self._stop_event.is_set() and self._serial is not None:
            try:
                # Look for sync bytes
                if not self._find_sync():
                    continue
                
                # Read packet length (2 bytes, little-endian)
                length_bytes = self._serial.read(2)
                if len(length_bytes) != 2:
                    continue
                
                length = struct.unpack('<H', length_bytes)[0]
                
                # Sanity check on length (max ~4KB per packet)
                if length == 0 or length > 4096:
                    continue
                
                # Read audio data
                audio_data = self._serial.read(length)
                if len(audio_data) != length:
                    continue
                
                # Read checksum
                checksum_bytes = self._serial.read(1)
                if len(checksum_bytes) != 1:
                    continue
                
                expected_checksum = checksum_bytes[0]
                actual_checksum = sum(audio_data) & 0xFF
                
                if expected_checksum != actual_checksum:
                    self.packets_corrupted += 1
                    continue
                
                # Valid packet - add to buffer
                with self._buffer_lock:
                    self._buffer.extend(audio_data)
                    # Trim buffer if too large
                    if len(self._buffer) > self.max_buffer_bytes:
                        trim_amount = len(self._buffer) - self.max_buffer_bytes
                        self._buffer = self._buffer[trim_amount:]
                
                self.packets_received += 1
                self.bytes_received += length
                
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
