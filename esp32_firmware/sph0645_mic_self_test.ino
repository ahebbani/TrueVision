/*
 * SPH0645 I2S Microphone Self-Test (ESP32)
 *
 * Goal: verify the mic is wired correctly and that the ESP32 is capturing audio.
 *
 * What it does:
 * - Reads I2S samples continuously
 * - Converts to 16-bit PCM
 * - Prints RMS + peak levels periodically over USB serial
 *
 * Wiring (typical SPH0645 breakout):
 * - 3V3  -> ESP32 3V3
 * - GND  -> ESP32 GND
 * - BCLK -> ESP32 GPIO 16 (I2S BCLK)
 * - LRCL -> ESP32 GPIO 17 (I2S WS/LRCLK)
 * - DOUT -> ESP32 GPIO 5  (I2S DATA IN)
 * - SEL/LR (if present) -> GND (LEFT)  OR  3V3 (RIGHT)
 *
 * Notes:
 * - Keep I2S wires short (<10cm if possible).
 * - If levels are extremely low/high, adjust SAMPLE_SHIFT.
 */

#include <driver/i2s.h>

// -------------------- User config --------------------
#define I2S_PORT            I2S_NUM_0
#define SAMPLE_RATE_HZ      16000
#define BUFFER_SAMPLES      512

// Choose I2S pins (avoid ESP32 strapping pins when possible)
#define PIN_I2S_BCLK        16
#define PIN_I2S_WS          17
#define PIN_I2S_DIN         5

// Many I2S mics (including SPH0645) output 24-bit data inside 32-bit words.
// Adjust if needed: common values are 11, 13, 14, 16.
#define SAMPLE_SHIFT        14

// Which channel to read (SEL pin decides which channel the mic outputs)
// If SEL=GND (LEFT), use ONLY_LEFT. If SEL=3V3 (RIGHT), use ONLY_RIGHT.
#define CHANNEL_FORMAT      I2S_CHANNEL_FMT_ONLY_LEFT
// -----------------------------------------------------

static int32_t i2s_raw[BUFFER_SAMPLES];
static int16_t pcm16[BUFFER_SAMPLES];

void setup_i2s() {
  i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE_HZ,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = CHANNEL_FORMAT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 4,
    .dma_buf_len = 512,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };

  i2s_pin_config_t pin_config = {
    .bck_io_num = PIN_I2S_BCLK,
    .ws_io_num = PIN_I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = PIN_I2S_DIN
  };

  esp_err_t err = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  if (err != ESP_OK) {
    Serial.printf("I2S install failed: %d\n", err);
    while (true) delay(1000);
  }

  err = i2s_set_pin(I2S_PORT, &pin_config);
  if (err != ESP_OK) {
    Serial.printf("I2S set pin failed: %d\n", err);
    while (true) delay(1000);
  }

  i2s_zero_dma_buffer(I2S_PORT);
}

void setup() {
  Serial.begin(115200);
  delay(200);

  Serial.println();
  Serial.println("SPH0645 mic self-test starting...");
  Serial.printf("Sample rate: %d Hz\n", SAMPLE_RATE_HZ);
  Serial.printf("Pins: BCLK=%d WS=%d DIN=%d\n", PIN_I2S_BCLK, PIN_I2S_WS, PIN_I2S_DIN);
  Serial.printf("Shift: >> %d\n", SAMPLE_SHIFT);

  setup_i2s();
  Serial.println("I2S initialized. Speak near the mic; RMS/peak should change.");
}

void loop() {
  size_t bytes_read = 0;
  esp_err_t result = i2s_read(I2S_PORT, i2s_raw, sizeof(i2s_raw), &bytes_read, portMAX_DELAY);
  if (result != ESP_OK || bytes_read == 0) return;

  const size_t samples_read = bytes_read / sizeof(int32_t);

  int32_t peak = 0;
  int64_t sum_sq = 0;

  for (size_t i = 0; i < samples_read && i < BUFFER_SAMPLES; i++) {
    const int32_t s32 = i2s_raw[i];
    const int16_t s16 = (int16_t)(s32 >> SAMPLE_SHIFT);
    pcm16[i] = s16;

    const int32_t a = (s16 >= 0) ? s16 : -s16;
    if (a > peak) peak = a;
    sum_sq += (int64_t)s16 * (int64_t)s16;
  }

  // Compute RMS
  double rms = 0.0;
  if (samples_read > 0) {
    rms = sqrt((double)sum_sq / (double)samples_read);
  }

  static uint32_t last_print_ms = 0;
  const uint32_t now = millis();
  if (now - last_print_ms >= 250) {
    last_print_ms = now;

    // Simple bar meter from peak
    int bars = (int)((peak / 32768.0) * 40.0);
    if (bars < 0) bars = 0;
    if (bars > 40) bars = 40;

    Serial.printf("RMS=%7.1f  PEAK=%5d  |", rms, (int)peak);
    for (int i = 0; i < bars; i++) Serial.print('#');
    Serial.println();
  }
}
