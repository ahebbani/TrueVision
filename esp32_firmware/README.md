# ESP32 TrueVision UART Firmware

The main firmware is `truevision_main.ino`. It streams microphone audio to the Raspberry Pi via UART and also handles mode control, the user button, and optional debug LEDs.

Other sketches in this folder are for isolated testing only:

- `test_audio_generator.ino` simulates audio packets without a microphone
- `uart0_pi_probe_test.ino` sends simple UART probe traffic
- `i2s_uart_audio_streamer.ino` is the older single-purpose audio streamer

## Hardware Required

- **ESP32 Development Board** (any variant with I2S support)
- **I2S MEMS Microphone** (recommended: INMP441, ICS-43434, or SPH0645)
- **Jumper wires** for connections

## Microphone Wiring

Connect your I2S microphone to the ESP32:

| I2S Mic Pin | ESP32 Pin | Description |
|-------------|-----------|-------------|
| SCK (BCLK)  | GPIO 14   | Serial Clock |
| WS (LRCLK)  | GPIO 15   | Word Select (Left/Right) |
| SD (DOUT)   | GPIO 32   | Serial Data |
| VDD         | 3.3V      | Power |
| GND         | GND       | Ground |

**Note:** Some microphones have an additional L/R or SEL pin:
- Connect to GND for left channel
- Connect to 3.3V for right channel
- Leave floating for default (usually left)

## Raspberry Pi UART Connection

Connect ESP32 to Raspberry Pi:

| ESP32 Pin | Pi Pin (Physical) | Pi GPIO | Description |
|-----------|-------------------|---------|-------------|
| GPIO 1 (TX) | Pin 10 | GPIO 15 | ESP32 transmits audio |
| GPIO 3 (RX) | Pin 8  | GPIO 14 | (Optional - not used) |
| GND | Pin 6, 9, 14, 20, 25, 30, 34, or 39 | GND | Common ground |

## Installation Instructions

### 1. Install Arduino IDE & ESP32 Support

**Install Arduino IDE:**
```bash
# Download from https://www.arduino.cc/en/software
# Or use Arduino IDE 2.x
```

**Add ESP32 Board Support:**
1. Open Arduino IDE
2. Go to **File → Preferences**
3. Add to "Additional Board Manager URLs":
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
4. Go to **Tools → Board → Boards Manager**
5. Search for "esp32"
6. Install "esp32 by Espressif Systems"

### 2. Configure Arduino IDE

**Select Board:**
- **Tools → Board → ESP32 Arduino → ESP32 Dev Module**
  (or your specific ESP32 board variant)

**Configure Settings:**
- **Tools → Upload Speed:** 921600
- **Tools → CPU Frequency:** 240MHz (recommended)
- **Tools → Flash Frequency:** 80MHz
- **Tools → Flash Mode:** QIO
- **Tools → Flash Size:** 4MB (or your board's size)
- **Tools → Partition Scheme:** Default 4MB with spiffs
- **Tools → Port:** Select your ESP32's USB port (e.g., /dev/ttyUSB0)

### 3. Select the firmware profile and upload the sketch

1. Open `truevision_main.ino` in Arduino IDE
2. Connect ESP32 to your computer via USB
3. Click **Upload** button (or press Ctrl+U)
4. Wait for compilation and upload to complete

Before uploading, set the board profile near the top of the sketch:

```cpp
#define BOARD_PROFILE_TEST        1
#define BOARD_PROFILE_PRODUCTION  2

#ifndef BOARD_PROFILE
#define BOARD_PROFILE BOARD_PROFILE_PRODUCTION
#endif
```

Use these profiles:

- `BOARD_PROFILE_TEST`: for the testing ESP32 that does not have the user button, mode switch, or debug LEDs connected
- `BOARD_PROFILE_PRODUCTION`: for the production ESP32 with the user button, mode switch, and two debug LEDs

Both profiles boot in `BOTH` mode.

Behavior by profile:

- Test profile:
  - boots in `BOTH`
  - no hardware mode/button interaction expected
  - best paired with `make run-esp32` on the Pi
- Production profile:
  - boots in `BOTH`
  - mode switch selects `AUDIO`-only or `FACE`-only
  - single short press sends a marker
  - double short press returns to `BOTH`
  - long press requests diagnostics from the Pi

**Troubleshooting Upload Issues:**
- If upload fails, try holding the **BOOT** button during upload
- Check that the correct port is selected
- Verify USB cable supports data (not just power)

### 4. Adjust Microphone Bit Shift

Different I2S microphones output data in different bit positions. The default shift is `>> 14`:

```cpp
audio_buffer[i] = (int16_t)(i2s_read_buffer[i] >> 14);
```

**If audio is too quiet or too loud, try adjusting:**
- `>> 11` - Louder (for mics that use lower bits)
- `>> 14` - Default (works for most INMP441)
- `>> 16` - Quieter (for mics that use upper bits)

**To adjust:** Edit line ~135 in the sketch and re-upload.

### 5. Test the Setup

**Monitor Serial Output:**
```bash
# truevision_main.ino uses UART0 for binary packets.
# Do not leave Serial Monitor attached while testing against the Pi.
# If you open Serial Monitor at 921600, you should expect binary garbage,
# not human-readable log messages.
```

**Test Audio Streaming:**
Once uploaded and connected to Raspberry Pi:
```bash
# On Raspberry Pi, run from the repo root:
make run-esp32
```

If you need the Pi to ignore firmware mode packets and force simultaneous face recognition + transcription, run:

```bash
make run-esp32-force-both
```

For Pi-side overrides without touching the ESP32 switch state:

- `make run-audio` forces audio-only mode and uses ESP32 UART audio.
- `make run-face` forces face-only mode and disables audio/transcription.

## Troubleshooting

### No Audio / Silent Output
- Check I2S microphone wiring (especially SCK, WS, SD pins)
- Verify microphone power (3.3V and GND connected)
- Try different bit shift values (>> 11, >> 14, >> 16)
- Check if microphone L/R pin is properly set

### Noisy Audio
- Keep wires short (< 10cm for I2S connections)
- Use twisted pair or shielded cable for I2S clock lines
- Add 100nF capacitor between microphone VDD and GND
- Try lowering sample rate to 8000 Hz (edit I2S_SAMPLE_RATE)

### UART Connection Issues
- Verify TX/RX aren't swapped (ESP32 TX → Pi RX)
- Confirm GND is connected between ESP32 and Pi
- Check that Pi UART is enabled (see main SETUP_PI.md)
- Try lower baud rate (460800 or 115200) if 921600 is unstable

### Mode / button behavior issues
- If the testing board appears to react to missing controls, confirm you flashed `BOARD_PROFILE_TEST`
- If production never changes modes, confirm the switch is wired to the pins used by `truevision_main.ino`
- If you always want the Pi to run both subsystems regardless of firmware mode packets, use `make run-esp32-force-both`

### Garbled Serial Monitor Output
- This is normal when streaming binary audio data
- Serial monitor shows raw audio bytes (appears as garbage)
- The Raspberry Pi receiver will decode it properly

## Pin Customization

If you need different pins (e.g., conflicts with other peripherals), edit these lines:

```cpp
// I2S Pins
#define I2S_SCK_PIN       14  // Change to your SCK pin
#define I2S_WS_PIN        15  // Change to your WS pin
#define I2S_SD_PIN        32  // Change to your SD pin
```

**Available GPIO pins for I2S on ESP32:**
- Most GPIO pins work, but avoid: 0, 2, 6-11 (used for boot/flash)
- Recommended: 12-15, 25-27, 32-33

## Performance Notes

- **Latency:** ~50-100ms end-to-end
- **CPU Usage:** ~5-10% on ESP32 @ 240MHz
- **Power:** ~80-100mA @ 3.3V during streaming
- **Packet Rate:** ~31 packets/second (512 samples @ 16kHz)

## Advanced Configuration

### Change Sample Rate

Edit line ~30:
```cpp
#define I2S_SAMPLE_RATE   16000  // Try 8000 or 44100
```

**Note:** Raspberry Pi receiver expects 16kHz. If you change this, update `esp32_serial_audio.py`:
```python
SAMPLE_RATE = 16000  # Match ESP32 setting
```

### Change Packet Size

Edit line ~39:
```cpp
#define BUFFER_SIZE       512  // Try 256 (faster) or 1024 (fewer packets)
```

Smaller = lower latency, more packets. Larger = higher throughput, fewer packets.

## References

- [ESP32 I2S Documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/peripherals/i2s.html)
- [INMP441 Datasheet](https://invensense.tdk.com/wp-content/uploads/2015/02/INMP441.pdf)
- [I2S Protocol Overview](https://en.wikipedia.org/wiki/I%C2%B2S)
