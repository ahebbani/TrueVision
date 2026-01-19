#!/usr/bin/env python3
"""
Test script to verify ESP32 serial audio reception.

This script connects to the ESP32 and:
1. Receives audio packets
2. Validates packet structure and checksums
3. Saves audio to a WAV file for playback/inspection
4. Prints statistics about the connection

Usage:
    python test_esp32_audio.py [--port /dev/serial0] [--duration 10]
"""
import argparse
import os
import sys
import time
from datetime import datetime

# Add parent directory to path so we can import from audio_analysis
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_analysis.esp32_serial_audio import ESP32SerialAudioReceiver


def main():
    parser = argparse.ArgumentParser(description='Test ESP32 serial audio reception')
    parser.add_argument('--port', default='/dev/serial0', help='Serial port (default: /dev/serial0)')
    parser.add_argument('--baud', type=int, default=921600, help='Baud rate (default: 921600)')
    parser.add_argument('--duration', type=float, default=10.0, help='Recording duration in seconds (default: 10)')
    parser.add_argument('--output', default=None, help='Output WAV file path (default: auto-generated)')
    args = parser.parse_args()
    
    print("=" * 60)
    print("ESP32 Serial Audio Test")
    print("=" * 60)
    print(f"Port: {args.port}")
    print(f"Baud Rate: {args.baud}")
    print(f"Duration: {args.duration} seconds")
    print()
    
    # Create receiver
    try:
        receiver = ESP32SerialAudioReceiver(
            port=args.port,
            baud_rate=args.baud,
            buffer_seconds=60.0
        )
    except Exception as e:
        print(f"ERROR: Failed to create receiver: {e}")
        print("\nTroubleshooting:")
        print("1. Check that ESP32 is connected and powered")
        print("2. Verify serial port is correct (try: ls /dev/tty*)")
        print("3. Ensure UART is enabled on Pi (see SETUP_PI.md)")
        print("4. Check that pyserial is installed (pip install pyserial)")
        return 1
    
    # Start receiving
    try:
        receiver.start()
        print("✓ Receiver started successfully")
        print(f"Receiving audio for {args.duration} seconds...")
        print()
        
        # Show progress
        start_time = time.time()
        last_packets = 0
        last_bytes = 0
        
        while time.time() - start_time < args.duration:
            time.sleep(1.0)
            elapsed = time.time() - start_time
            
            # Calculate rates
            packets_delta = receiver.packets_received - last_packets
            bytes_delta = receiver.bytes_received - last_bytes
            last_packets = receiver.packets_received
            last_bytes = receiver.bytes_received
            
            buffer_duration = receiver.get_buffer_duration()
            
            print(f"[{elapsed:5.1f}s] "
                  f"Packets: {receiver.packets_received:4d} (+{packets_delta:2d}/s) | "
                  f"Corrupted: {receiver.packets_corrupted:3d} | "
                  f"Buffer: {buffer_duration:5.1f}s | "
                  f"Rate: {bytes_delta/1024:.1f} KB/s")
        
        print()
        print("✓ Recording complete")
        
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\nERROR during reception: {e}")
        return 1
    
    # Generate output filename if not specified
    if args.output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"esp32_test_audio_{timestamp}.wav"
    
    # Save to WAV file
    print(f"\nSaving audio to: {args.output}")
    success = receiver.write_to_wav(args.output)
    
    if success:
        print("✓ WAV file saved successfully")
        file_size = os.path.getsize(args.output)
        print(f"  File size: {file_size / 1024:.1f} KB")
        print(f"  Duration: {receiver.get_buffer_duration():.1f} seconds")
        print()
        print("You can play it with: aplay " + args.output)
        print("Or on desktop: ffplay " + args.output)
    else:
        print("✗ Failed to save WAV file")
        return 1
    
    # Stop receiver
    receiver.stop()
    
    # Print summary statistics
    print()
    print("=" * 60)
    print("Summary Statistics")
    print("=" * 60)
    print(f"Total packets received: {receiver.packets_received}")
    print(f"Corrupted packets: {receiver.packets_corrupted}")
    if receiver.packets_received > 0:
        error_rate = 100.0 * receiver.packets_corrupted / (receiver.packets_received + receiver.packets_corrupted)
        print(f"Error rate: {error_rate:.2f}%")
    print(f"Total bytes received: {receiver.bytes_received:,}")
    print(f"Average data rate: {receiver.bytes_received / args.duration / 1024:.1f} KB/s")
    print()
    
    if receiver.packets_received == 0:
        print("⚠ WARNING: No packets received!")
        print("\nTroubleshooting:")
        print("1. Check ESP32 is running the audio firmware")
        print("2. Verify wiring: ESP32 TX → Pi RX, GND → GND")
        print("3. Try lower baud rate: --baud 460800 or --baud 115200")
        print("4. Check ESP32 serial monitor for error messages")
        return 1
    
    if receiver.packets_corrupted > receiver.packets_received * 0.1:
        print("⚠ WARNING: High corruption rate (>10%)")
        print("\nRecommendations:")
        print("1. Use shorter/better quality wires")
        print("2. Try lower baud rate: --baud 460800")
        print("3. Ensure good GND connection")
        print("4. Check for electrical interference")
    
    print("✓ Test completed successfully!")
    return 0


if __name__ == '__main__':
    sys.exit(main())
