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
 * Button wiring:
 *   GPIO 22 → one leg of momentary push-button
 *   GND     → other leg
 *   (uses INPUT_PULLUP — no external resistor needed)
 *
 *   Released (HIGH) = FACE mode
 *   Pressed  (LOW)  = AUDIO mode
 *
 * I2S Microphone wiring:
 *   SCK  (BCLK)  → GPIO 16
 *   WS   (LRCLK) → GPIO 17
 *   SD   (DOUT)  → GPIO 5
 *   SEL/LR       → GND
 *   VDD → 3.3V, GND → GND
 *
 * UART:
 *   ESP32 TX (GPIO 1) → Pi RX (physical pin 10 / GPIO 15)
 *   ESP32 GND          → Pi GND
 */

#include <driver/i2s.h>

// ─── Pins ────────────────────────────────────────────────────────────────────
#define MODE_BUTTON_PIN   22

#define I2S_SCK_PIN       16
#define I2S_WS_PIN        17
#define I2S_SD_PIN        5

// ─── I2S ─────────────────────────────────────────────────────────────────────
#define I2S_PORT          I2S_NUM_0
#define I2S_SAMPLE_RATE   16000
#define SAMPLE_SHIFT      14       // right-shift 32-bit I2S → 16-bit
#define BUFFER_SIZE       256      // samples per packet

// ─── UART ────────────────────────────────────────────────────────────────────
#define UART_BAUD         921600
static HardwareSerial &UART0 = Serial0;

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

// ─── State ───────────────────────────────────────────────────────────────────
static uint8_t  current_mode   = MODE_FACE;
static bool     btn_last       = true;   // HIGH = released (pullup)
static uint32_t btn_change_ms  = 0;
#define DEBOUNCE_MS 50

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
    UART0.write(buf, len);
}

// ─── setup() ─────────────────────────────────────────────────────────────────

void setup() {
    // Button
    pinMode(MODE_BUTTON_PIN, INPUT_PULLUP);
    btn_last = (digitalRead(MODE_BUTTON_PIN) == HIGH);
    current_mode = btn_last ? MODE_FACE : MODE_AUDIO;

    // UART
    UART0.begin(UART_BAUD);
    delay(100);

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
    // ── Check button ─────────────────────────────────────────────────────
    bool btn_now = (digitalRead(MODE_BUTTON_PIN) == HIGH);
    if (btn_now != btn_last && (millis() - btn_change_ms) > DEBOUNCE_MS) {
        btn_last = btn_now;
        btn_change_ms = millis();
        uint8_t new_mode = btn_now ? MODE_FACE : MODE_AUDIO;
        if (new_mode != current_mode) {
            current_mode = new_mode;
            send_mode(current_mode);
        }
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
    UART0.write(pkt_buf, pkt_len);
}
