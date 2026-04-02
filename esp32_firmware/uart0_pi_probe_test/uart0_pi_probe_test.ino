/*
  UART0 (TX0=GPIO1) definitive test for Raspberry Pi reception

  Purpose:
    Prove the ESP32 is driving bytes out on UART0 TX0 (GPIO1) and that the
    Raspberry Pi can receive them on its RXD pin.

  Wiring (ESP32 <-> Raspberry Pi):
    - ESP32 TX0 (GPIO1 / pin labeled TX)  -> Pi RXD (physical pin 10 / GPIO15)
    - ESP32 GND                            -> Pi GND
    - (Optional) ESP32 RX0 (GPIO3)         -> Pi TXD (physical pin 8 / GPIO14)

  Expected on Pi:
    python probe_uart.py --port /dev/serial0 --baud 115200 --seconds 2 --show 200

  You should see:
    - bytes_read > 0
    - sync_AA55_seen=True
    - hex output containing repeated: AA 55
    - ASCII banner and timestamp lines
*/

static const uint32_t BAUD = 115200;

void setup() {
  // Force output on UART0 pins (TX0=GPIO1, RX0=GPIO3)
  Serial0.begin(BAUD);
  delay(200);

  // Readable banner (will appear in the Pi's hex dump as ASCII bytes)
  Serial0.println("ESP32 UART0 TEST START");
}

void loop() {
  // Emit the exact sync bytes your Pi receiver expects
  Serial0.write((uint8_t)0xAA);
  Serial0.write((uint8_t)0x55);

  // Add recognizable ASCII payload
  Serial0.print(" T=");
  Serial0.println(millis());

  delay(100);
}
