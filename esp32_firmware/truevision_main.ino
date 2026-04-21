/*
 * TrueVision ESP32 — Simplified TX-Only Firmware
 *
 * Design:
 *   - Always streams I2S audio to the Pi via UART (never pauses)
 *   - Sends MODE_CHANGE packet when the button state changes
 *   - Does NOT receive anything from the Pi (no heartbeat, no RX parsing)
 *   - Single-threaded: setup() + loop(), no FreeRTOS tasks
 *
 * The Pi decides what to do with the audio based on the current mode.
 *
 * Protocol frame (TX only):
 *   [0xAA][0x55][TYPE(1)][LEN_LO][LEN_HI][DATA(LEN)][CHECKSUM]
 *   Checksum = sum(data bytes) & 0xFF
 *
 *   0x01  AUDIO_DATA    raw int16 PCM at 16 kHz mono
 *   0x02  MODE_CHANGE   1-byte: 0x00=AUDIO, 0x01=FACE
 *
 * Mode switch wiring:
 *   GPIO 35 → one side of two-position mode switch
 *   GPIO 36 → other side of two-position mode switch
 *   (external wiring should assert the selected side; pins 35/36 are
 *    input-only on many ESP32 modules so provide external pull-ups/downs)
 *
 *   When the switch selects the AUDIO side the board announces AUDIO mode
 *   When the switch selects the FACE side the board announces FACE mode
 *
 * I2S Microphone wiring (updated):
 *   SCK  (BCLK)  → GPIO 8
 *   WS   (LRCLK) → GPIO 6
 *   SD   (DOUT)  → GPIO 7
 *   SEL/LR       → GND
 *   VDD → 3.3V, GND → GND
 *
 * UART (updated):
 *   ESP32 TX (GPIO 17) → Pi RX
 *   ESP32 RX (GPIO 18) ← Pi TX
 *   ESP32 GND          → Pi GND
 */

#include <driver/i2s.h>

// ─── Pins ────────────────────────────────────────────────────────────────────
// Two-pin mode switch (either side indicates the selected mode)
#define MODE_SWITCH_A_PIN 35
#define MODE_SWITCH_B_PIN 36

// I2S microphone (updated pins)
#define I2S_SCK_PIN       8
#define I2S_WS_PIN        6
#define I2S_SD_PIN        7

// ─── I2S ─────────────────────────────────────────────────────────────────────
#define I2S_PORT          I2S_NUM_0
#define I2S_SAMPLE_RATE   16000
#define SAMPLE_SHIFT      14       // right-shift 32-bit I2S → 16-bit
#define BUFFER_SIZE       256      // samples per packet

// ─── UART ────────────────────────────────────────────────────────────────────
#define UART_BAUD         921600
// Use a hardware serial instance that can be bound to arbitrary TX/RX pins
#define UART_TX_PIN       17
#define UART_RX_PIN       18
HardwareSerial UARTSerial(2);

// ─── Protocol ────────────────────────────────────────────────────────────────
#define SYNC_1            0xAA
#define SYNC_2            0x55
#define PKT_AUDIO         0x01
#define PKT_MODE_CHANGE   0x02
#define MODE_AUDIO        0x00
#define MODE_FACE         0x01

// ─── Buffers ─────────────────────────────────────────────────────────────────
static int32_t  i2s_raw[BUFFER_SIZE];
static int16_t  pcm[BUFFER_SIZE];
static uint8_t  pkt_buf[BUFFER_SIZE * 2 + 6];

// ─── Debug LEDs ─────────────────────────────────────────────────────────────
// Heartbeat LED to show firmware is running, and packet LED to show audio sends.
#define LED_HEART_PIN 9
#define LED_PKT_PIN   10

// ─── State ───────────────────────────────────────────────────────────────────
#define DEBOUNCE_MS 50

static uint8_t  current_mode   = MODE_FACE;
static uint8_t  last_mode_sent = MODE_FACE;
static uint32_t btn_change_ms  = 0;
static uint32_t last_heartbeat_ms = 0;
static bool     heartbeat_state = false;

// ─── Helpers ─────────────────────────────────────────────────────────────────

static size_t build_packet(uint8_t *buf, uint8_t type,
                           const uint8_t *data, uint16_t data_len) {
    size_t i = 0;
    buf[i++] = SYNC_1;
    buf[i++] = SYNC_2;
    buf[i++] = type;
    buf[i++] = (uint8_t)(data_len & 0xFF);
    buf[i++] = (uint8_t)((data_len >> 8) & 0xFF);
    uint8_t cs = 0;
    for (uint16_t j = 0; j < data_len; j++) {
        buf[i++] = data[j];
        cs += data[j];
    }
    buf[i++] = cs;
    return i;
}

static void send_mode(uint8_t mode) {
    uint8_t buf[8];
    uint8_t payload = mode;
    size_t len = build_packet(buf, PKT_MODE_CHANGE, &payload, 1);
    UARTSerial.write(buf, len);
}

// ─── setup() ─────────────────────────────────────────────────────────────────

void setup() {
    // Two-pin mode switch
    // Pins 35/36 are input-only on many ESP32s; rely on external wiring for pull-ups/downs.
    pinMode(MODE_SWITCH_A_PIN, INPUT);
    pinMode(MODE_SWITCH_B_PIN, INPUT);
    // Initialize current mode from switch state; prefer explicit pin assertions.
    bool a = digitalRead(MODE_SWITCH_A_PIN);
    bool b = digitalRead(MODE_SWITCH_B_PIN);
    if (a && !b) {
        current_mode = MODE_AUDIO;
    } else if (b && !a) {
        current_mode = MODE_FACE;
    } else {
        // ambiguous: keep FACE as default
        current_mode = MODE_FACE;
    }
    last_mode_sent = current_mode;

    // UART
    // Initialize UART on requested pins (RX, TX)
    UARTSerial.begin(UART_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);
    delay(100);

    // Debug LEDs
    pinMode(LED_HEART_PIN, OUTPUT);
    pinMode(LED_PKT_PIN, OUTPUT);
    digitalWrite(LED_HEART_PIN, LOW);
    digitalWrite(LED_PKT_PIN, LOW);

    // I2S microphone
    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
    cfg.sample_rate = I2S_SAMPLE_RATE;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
    cfg.communication_format = I2S_COMM_FORMAT_I2S;
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = 4;
    cfg.dma_buf_len = 1024;
    cfg.use_apll = false;

    i2s_driver_install(I2S_PORT, &cfg, 0, NULL);

    i2s_pin_config_t pins = {};
    pins.bck_io_num = I2S_SCK_PIN;
    pins.ws_io_num = I2S_WS_PIN;
    pins.data_in_num = I2S_SD_PIN;
    pins.data_out_num = I2S_PIN_NO_CHANGE;
    i2s_set_pin(I2S_PORT, &pins);

    // Announce initial mode
    send_mode(current_mode);
}

// ─── loop() ──────────────────────────────────────────────────────────────────

void loop() {
    // ── Check two-pin mode switch ────────────────────────────────────────
    bool a = digitalRead(MODE_SWITCH_A_PIN);
    bool b = digitalRead(MODE_SWITCH_B_PIN);
    uint8_t new_mode = current_mode;
    // Priority: if A asserted -> AUDIO; else if B asserted -> FACE; else keep current
    if (a && !b) {
        new_mode = MODE_AUDIO;
    } else if (b && !a) {
        new_mode = MODE_FACE;
    }
    if (new_mode != current_mode && (millis() - btn_change_ms) > DEBOUNCE_MS) {
        current_mode = new_mode;
        btn_change_ms = millis();
        send_mode(current_mode);
    }

    // Heartbeat LED (non-blocking)
    uint32_t now = millis();
    if (now - last_heartbeat_ms >= 500) {
        last_heartbeat_ms = now;
        heartbeat_state = !heartbeat_state;
        digitalWrite(LED_HEART_PIN, heartbeat_state ? HIGH : LOW);
    }

    // ── Read I2S and send audio ──────────────────────────────────────────
    size_t bytes_read = 0;
    esp_err_t rc = i2s_read(I2S_PORT, i2s_raw, sizeof(i2s_raw),
                            &bytes_read, pdMS_TO_TICKS(100));
    if (rc != ESP_OK || bytes_read == 0) {
        return;  // retry next loop
    }

    size_t samples = bytes_read / sizeof(int32_t);
    for (size_t i = 0; i < samples && i < BUFFER_SIZE; i++) {
        pcm[i] = (int16_t)(i2s_raw[i] >> SAMPLE_SHIFT);
    }

    uint16_t audio_bytes = (uint16_t)(samples * sizeof(int16_t));
    size_t pkt_len = build_packet(pkt_buf, PKT_AUDIO,
                                  (const uint8_t *)pcm, audio_bytes);
    // Pulse packet LED briefly to indicate a packet send
    digitalWrite(LED_PKT_PIN, HIGH);
    UARTSerial.write(pkt_buf, pkt_len);
    // short visible pulse without significantly delaying streaming
    delayMicroseconds(2000);
    digitalWrite(LED_PKT_PIN, LOW);
}
