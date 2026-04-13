/*
 * SPH0645 I2S Microphone Self-Test (ESP32)
 *
 * Goal: verify the mic is wired correctly and that the ESP32 is capturing audio.
 *
 * What it does:
 * - Reads I2S samples continuously
 * - Converts to 16-bit PCM
 * - Prints RMS + peak levels periodically over USB serial
 * - Tests the mode switch and push button using the same pin mappings and
 *   timing rules as truevision_main.ino
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
 * - BOARD_PROFILE defaults to the test-board profile, matching truevision_main.
 */

#include <driver/i2s.h>

// -------------------- User config --------------------
#define I2S_PORT            I2S_NUM_0
#define SAMPLE_RATE_HZ      16000
#define BUFFER_SAMPLES      512

#define BUTTON_PIN          11

#define BOARD_PROFILE_TEST        1
#define BOARD_PROFILE_PRODUCTION  2

#ifndef BOARD_PROFILE
#define BOARD_PROFILE BOARD_PROFILE_TEST
#endif

#if BOARD_PROFILE == BOARD_PROFILE_TEST
#define ENABLE_MARKER_BUTTON  0
#define ENABLE_MODE_SWITCH    1
#define MODE_PIN_A            22
#define MODE_PIN_B            23
#define MODE_PIN_MODE         INPUT_PULLUP
#elif BOARD_PROFILE == BOARD_PROFILE_PRODUCTION
#define ENABLE_MARKER_BUTTON  1
#define ENABLE_MODE_SWITCH    1
#define MODE_PIN_A            35
#define MODE_PIN_B            36
#define MODE_PIN_MODE         INPUT
#else
#error "Unsupported BOARD_PROFILE"
#endif

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

#define MODE_AUDIO          0x00
#define MODE_FACE           0x01
#define MODE_BOTH           0x02

#define BUTTON_DEBOUNCE_MS     50
#define BUTTON_DOUBLE_PRESS_MS 600
#define BUTTON_LONG_PRESS_MS   3000
// -----------------------------------------------------

static int32_t i2s_raw[BUFFER_SAMPLES];
static int16_t pcm16[BUFFER_SAMPLES];
static uint8_t switch_mode = MODE_BOTH;
static uint8_t last_valid_mode = MODE_BOTH;
static bool mode_invalid = false;

static bool switch_have_valid_pos = false;
static bool switch_last_a = false;
static bool switch_last_b = false;

#if ENABLE_MARKER_BUTTON
static bool button_stable = true;
static uint32_t button_change_ms = 0;
static uint32_t button_press_ms = 0;
static bool button_long_done = false;
static bool button_short_pending = false;
static uint32_t button_short_ms = 0;
#endif

static const char *mode_name(uint8_t mode) {
  switch (mode) {
    case MODE_AUDIO: return "AUDIO";
    case MODE_FACE:  return "FACE";
    case MODE_BOTH:  return "BOTH";
    default:         return "UNKNOWN";
  }
}

static uint8_t resolve_switch_mode(bool pin_a, bool pin_b, bool *valid) {
  if (pin_a && !pin_b) {
    if (valid) *valid = true;
    return MODE_AUDIO;
  }
  if (!pin_a && pin_b) {
    if (valid) *valid = true;
    return MODE_FACE;
  }
  if (valid) *valid = false;
  return last_valid_mode;
}

static void print_switch_state(bool pin_a, bool pin_b, bool valid, uint8_t mode) {
  if (valid) {
    Serial.printf("SWITCH: A=%d B=%d -> %s\n", pin_a ? 1 : 0, pin_b ? 1 : 0, mode_name(mode));
  } else {
    Serial.printf("SWITCH: A=%d B=%d -> INVALID (pins should not match)\n", pin_a ? 1 : 0, pin_b ? 1 : 0);
  }
}

static void poll_switch_and_button() {
  const uint32_t now = millis();

#if ENABLE_MODE_SWITCH
  const bool pin_a = (digitalRead(MODE_PIN_A) == HIGH);
  const bool pin_b = (digitalRead(MODE_PIN_B) == HIGH);

  bool valid = false;
  const uint8_t new_mode = resolve_switch_mode(pin_a, pin_b, &valid);

  if (!valid) {
    if (!mode_invalid) {
      mode_invalid = true;
      print_switch_state(pin_a, pin_b, false, new_mode);
    }
  } else if (!switch_have_valid_pos || pin_a != switch_last_a || pin_b != switch_last_b || mode_invalid) {
    switch_have_valid_pos = true;
    switch_last_a = pin_a;
    switch_last_b = pin_b;
    last_valid_mode = new_mode;
    switch_mode = new_mode;
    mode_invalid = false;
    print_switch_state(pin_a, pin_b, true, new_mode);
  }
#endif

#if ENABLE_MARKER_BUTTON
  const bool button_raw = (digitalRead(BUTTON_PIN) == HIGH);
  if (button_raw != button_stable) {
    if ((now - button_change_ms) >= BUTTON_DEBOUNCE_MS) {
      button_stable = button_raw;
      button_change_ms = now;
      if (!button_stable) {
        button_press_ms = now;
        button_long_done = false;
      } else {
        const uint32_t held = now - button_press_ms;
        if (!button_long_done && held >= BUTTON_DEBOUNCE_MS) {
          if (button_short_pending && (now - button_short_ms) <= BUTTON_DOUBLE_PRESS_MS) {
            button_short_pending = false;
            Serial.println("BUTTON: double press -> BOTH mode request");
          } else {
            button_short_pending = true;
            button_short_ms = now;
          }
        }
      }
    }
  } else {
    button_change_ms = now;
  }

  if (button_short_pending && (now - button_short_ms) > BUTTON_DOUBLE_PRESS_MS) {
    button_short_pending = false;
    Serial.println("BUTTON: short press -> marker request");
  }

  if (!button_stable && !button_long_done && (now - button_press_ms) >= BUTTON_LONG_PRESS_MS) {
    button_long_done = true;
    if (button_short_pending) {
      button_short_pending = false;
      Serial.println("BUTTON: pending short press canceled");
    }
    Serial.println("BUTTON: long press -> diagnostic request");
  }
#endif
}

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

#if ENABLE_MARKER_BUTTON
  pinMode(BUTTON_PIN, INPUT_PULLUP);
#endif
#if ENABLE_MODE_SWITCH
  pinMode(MODE_PIN_A, MODE_PIN_MODE);
  pinMode(MODE_PIN_B, MODE_PIN_MODE);
#endif

  Serial.println();
  Serial.println("SPH0645 mic self-test starting...");
  Serial.printf("Sample rate: %d Hz\n", SAMPLE_RATE_HZ);
  Serial.printf("Pins: BCLK=%d WS=%d DIN=%d\n", PIN_I2S_BCLK, PIN_I2S_WS, PIN_I2S_DIN);
  Serial.printf("Shift: >> %d\n", SAMPLE_SHIFT);
#if BOARD_PROFILE == BOARD_PROFILE_TEST
  Serial.println("Board profile: TEST");
#else
  Serial.println("Board profile: PRODUCTION");
#endif
#if ENABLE_MODE_SWITCH
  Serial.printf("Mode switch pins: A=%d B=%d\n", MODE_PIN_A, MODE_PIN_B);
#endif
#if ENABLE_MARKER_BUTTON
  Serial.printf("Button pin: %d (active low)\n", BUTTON_PIN);
#else
  Serial.println("Button test: disabled for this board profile");
#endif

  setup_i2s();
  Serial.println("I2S initialized. Speak near the mic; RMS/peak should change.");

  poll_switch_and_button();
}

void loop() {
  poll_switch_and_button();

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
