/*
 * TrueVision ESP32 Main Sketch
 *
 * Handles:
 *   - I2S microphone capture + streaming to Raspberry Pi via UART
 *   - Mode switch: test board uses GPIO 22 / 23, production board uses its board-defined pins
 *   - Meeting-marker / BOTH-override push button (GPIO 11)
 *   - Dual debug LEDs (GPIO 9 / 10) with diagnostic patterns
 *   - Bidirectional UART control protocol with Raspberry Pi
 *
 * ── HARDWARE NOTE ──────────────────────────────────────────────────────────
 * On classic ESP32-WROOM-32, GPIOs 6–11 are wired to the internal SPI flash
 * and MUST NOT be driven externally.  If your PCB uses an ESP32-S3, ESP32-C3,
 * ESP32-S2, or a module with only 4-wire (QSPI) flash leaving 9/10/11 free,
 * you can ignore this warning.  Verify with your module datasheet before
 * flashing.
 * ───────────────────────────────────────────────────────────────────────────
 *
 * I2S Microphone Wiring (SPH0645 / INMP441 / ICS-43434):
 *   SCK  (BCLK)  → GPIO 16
 *   WS   (LRCLK) → GPIO 17
 *   SD   (DOUT)  → GPIO 5
 *   SEL/LR pin  → GND  (outputs LEFT channel → I2S_CHANNEL_FMT_ONLY_LEFT)
 *   VDD  → 3.3 V,  GND → GND
 *
 * UART to Raspberry Pi:
 *   ESP32 TX (GPIO 1)  → Pi RX  (Physical pin 10 / GPIO 15)
 *   ESP32 RX (GPIO 3)  → Pi TX  (Physical pin  8 / GPIO 14)
 *   ESP32 GND          → Pi GND (Physical pin  6)
 *
 * ── Extended Bidirectional Protocol ────────────────────────────────────────
 *   Frame:  [0xAA] [0x55] [TYPE(1)] [LEN_LO] [LEN_HI] [DATA(LEN)] [CHECKSUM]
 *   Checksum = sum(data bytes) & 0xFF
 *
 *   ESP32 → Pi types:
 *     0x01  AUDIO_DATA   raw int16 PCM at 16 kHz mono
 *     0x02  MODE_CHANGE  1-byte payload: 0x00=AUDIO  0x01=FACE  0x02=BOTH
 *     0x03  MARKER       0-byte payload (Pi timestamps on receipt)
 *     0x04  DIAG_REQUEST 0-byte payload (request Pi status report)
 *
 *   Pi → ESP32 types:
 *     0x10  HEARTBEAT    1-byte payload: 0x00
 *     0x11  PI_STATUS    1+ bytes: error_code + optional ASCII message
 *                        Only sent by Pi when its OLED hardware is absent,
 *                        or when responding to DIAG_REQUEST.
 *     0x12  ACK          1-byte payload: echoed TYPE of acknowledged packet
 *     0x13  FORCE_MODE   1-byte payload: 0x00=AUDIO  0x01=FACE  0x02=BOTH
 *     0x14  CLEAR_MODE   0-byte payload: clear any active Pi override
 *
 *   PI_STATUS error codes:
 *     0x00  OK
 *     0x01  CAMERA_FAIL
 *     0x02  MODEL_LOAD_FAIL
 *     0x03  DB_ERROR
 *     0x04  SUMMARIZER_TIMEOUT
 *
 * ── LED Diagnostic Patterns ────────────────────────────────────────────────
 *   LED1 (GPIO 9)  — Audio / hardware health
 *     OFF              Normal, I2S streaming OK
 *     1-blink / 3 s   Recoverable I2S read errors (still running)
 *     Fast blink 4 Hz  I2S driver init failed entirely
 *     Solid ON         Mic dead: all-zero samples for > 2 s
 *
 *   LED2 (GPIO 10) — Link / Pi health
 *     OFF              Normal, Pi heartbeat received within timeout
 *     1-blink / 3 s   Mode switch in invalid state (both pins identical)
 *     Slow blink 1 Hz  Pi heartbeat timeout (no 0x10 for > 8 s)
 *     Fast blink 4 Hz  UART TX overflow (write buffer near full)
 *     Solid ON         Pi reported critical error (0x11) or UART framing error
 *
 *   Both LEDs solid 2 s on boot  Previous reset was watchdog / panic
 *   Both LEDs alternating 4 Hz   I2S init failed AND Pi critical error
 *
 * ── Button (GPIO 11) ───────────────────────────────────────────────────────
 *   Single short press   Send MARKER packet → Pi inserts [MARKER HH:MM:SS]
 *                        into the active meeting transcript
 *   Double short press   Force BOTH mode (audio + face together)
 *   Long press  (≥ 3 s)  Send DIAG_REQUEST → Pi replies with PI_STATUS even
 *                        if its OLED is up; both LEDs flash 3× alternately
 *                        to confirm receipt of ACK
 *
 * Author: Aditya Hebbani
 * Date:   January 2026
 */

#include <driver/i2s.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>

// ─── Pin Definitions ─────────────────────────────────────────────────────────
// See hardware note at top of file regarding GPIO 9 / 10 / 11 on WROOM-32.
#define LED_AUDIO_PIN   9    // LED1: audio / hardware health
#define LED_LINK_PIN    10   // LED2: link / Pi health
#define BUTTON_PIN      11   // Push button, active-low (INPUT_PULLUP)

// I2S microphone
#define I2S_SCK_PIN     16   // BCLK
#define I2S_WS_PIN      17   // LRCLK / WS
#define I2S_SD_PIN      5    // DOUT

// ─── I2S Configuration ───────────────────────────────────────────────────────
#define I2S_PORT              I2S_NUM_0
#define I2S_SAMPLE_RATE       16000
#define I2S_BITS_PER_SAMPLE   I2S_BITS_PER_SAMPLE_32BIT
#define I2S_CHANNEL_FORMAT    I2S_CHANNEL_FMT_ONLY_LEFT
// Right-shift to extract 16-bit from 32-bit I2S frame.
// Common values for different mics: 11, 13, 14, 16.
#define SAMPLE_SHIFT          14
#define BUFFER_SIZE           512   // samples / packet  (32 ms @ 16 kHz)
#define DMA_BUFFER_COUNT      4
#define DMA_BUFFER_SIZE       1024

// ─── UART ────────────────────────────────────────────────────────────────────
#define UART_BAUD_RATE        921600
// GPIO 1 = TX0, GPIO 3 = RX0. On some boards Serial maps to USB CDC;
// Serial0 always maps to UART0 hardware pins.
static HardwareSerial &UART0 = Serial0;

// ─── Board Profile Selection ────────────────────────────────────────────────
// Keep one sketch source for both boards. Change BOARD_PROFILE (or override it
// via a compile flag) before flashing.
#define BOARD_PROFILE_TEST        1
#define BOARD_PROFILE_PRODUCTION  2

// Active default when no external compile flag overrides BOARD_PROFILE.
// Change this line before flashing if you want the test-board profile.
#ifndef BOARD_PROFILE
#define BOARD_PROFILE BOARD_PROFILE_TEST
#endif

#if BOARD_PROFILE == BOARD_PROFILE_TEST
#define ENABLE_STATUS_LEDS    0
#define ENABLE_MARKER_BUTTON  0
#define ENABLE_MODE_SWITCH    1
#define MODE_PIN_A            22   // Test-board switch leg A (uses internal pull-up)
#define MODE_PIN_B            23   // Test-board switch leg B (uses internal pull-up)
#define MODE_PIN_MODE         INPUT_PULLUP
#elif BOARD_PROFILE == BOARD_PROFILE_PRODUCTION
// The production hardware described for this project has one user button, a
// mode switch, and two programmable debug LEDs.
#define ENABLE_STATUS_LEDS    1
#define ENABLE_MARKER_BUTTON  1
#define ENABLE_MODE_SWITCH    1
#define MODE_PIN_A            35   // Production switch leg A
#define MODE_PIN_B            36   // Production switch leg B
#define MODE_PIN_MODE         INPUT
#else
#error "Unsupported BOARD_PROFILE"
#endif

// ─── Protocol ────────────────────────────────────────────────────────────────
#define SYNC_BYTE_1       0xAA
#define SYNC_BYTE_2       0x55
// ESP32 → Pi
#define PKT_AUDIO         0x01
#define PKT_MODE_CHANGE   0x02
#define PKT_MARKER        0x03
#define PKT_DIAG_REQUEST  0x04
// Pi → ESP32
#define PKT_HEARTBEAT     0x10
#define PKT_PI_STATUS     0x11
#define PKT_ACK           0x12
#define PKT_FORCE_MODE    0x13
#define PKT_CLEAR_MODE    0x14

// ─── Operating Modes ─────────────────────────────────────────────────────────
#define MODE_AUDIO  0x00   // Stream audio; Pi skips face recognition
#define MODE_FACE   0x01   // Pi runs face recognition; no audio stream sent
#define MODE_BOTH   0x02   // Stream audio and allow face recognition together

// ─── Timing ──────────────────────────────────────────────────────────────────
#define HEARTBEAT_TIMEOUT_MS   8000   // LED2 slow-blink after this long without Pi heartbeat
#define ZERO_SAMPLE_MS         2000   // LED1 solid after this long of all-zero samples
#define SUPERVISOR_TICK_MS     50     // Supervisor polling interval
#define BUTTON_DEBOUNCE_MS     50     // Hardware debounce window
#define BUTTON_DOUBLE_PRESS_MS 600    // Max gap between short presses for BOTH mode
#define BUTTON_LONG_PRESS_MS   3000   // Threshold for long press
#define UART_TX_LOW_WATER      128    // bytes; below this = TX overflow risk

// ─── Shared Buffers ──────────────────────────────────────────────────────────
static int32_t  s_i2s_raw[BUFFER_SIZE];
static int16_t  s_pcm[BUFFER_SIZE];
// Worst-case packet: sync(2) + type(1) + len(2) + audio(BUFFER_SIZE*2) + cs(1)
static uint8_t  s_audio_pkt[BUFFER_SIZE * 2 + 6];

// ─── Synchronisation ─────────────────────────────────────────────────────────
static SemaphoreHandle_t s_uart_tx_mutex;  // protects all UART writes

// ─── Shared State (written by single owner task; read by others) ──────────────
static volatile uint8_t  s_mode             = MODE_BOTH;
static volatile uint8_t  s_switch_mode      = MODE_BOTH;
static volatile uint8_t  s_last_valid_mode  = MODE_BOTH;
static volatile bool     s_mode_override_active = false;
static volatile uint8_t  s_mode_override    = MODE_BOTH;
static volatile bool     s_mode_invalid     = false;
static volatile uint32_t s_hb_last_ms       = 0;    // millis() of last heartbeat from Pi
static volatile bool     s_pi_critical      = false; // Pi sent non-zero PI_STATUS
static volatile bool     s_i2s_failed       = false; // I2S init hard-failed
static volatile bool     s_i2s_error        = false; // recoverable I2S read error (recent)
static volatile uint32_t s_i2s_err_ms       = 0;    // millis() of last recoverable error
static volatile bool     s_mic_dead         = false; // all-zero samples > ZERO_SAMPLE_MS
static volatile bool     s_uart_overflow    = false; // TX buffer near-full
static volatile bool     s_uart_framing     = false; // framing / checksum error on RX

// Flag set by UART_RX after receiving a valid DIAG ACK; Supervisor does the flash.
static volatile bool     s_diag_ack_pending = false;

// ─── LED Patterns ────────────────────────────────────────────────────────────
typedef enum : uint8_t {
    LED_OFF        = 0,
    LED_BLINK_1_3S = 1,   // 100 ms pulse every 3 s
    LED_SLOW_1HZ   = 2,   // 500 ms on / 500 ms off
    LED_FAST_4HZ   = 3,   // 125 ms on / 125 ms off
    LED_SOLID      = 4,
    LED_ALT_4HZ    = 5,   // for combined state: each LED is anti-phase 4 Hz
} LedPattern;

static volatile LedPattern s_led1 = LED_OFF;
static volatile LedPattern s_led2 = LED_OFF;

// ─── Helpers ─────────────────────────────────────────────────────────────────

static inline void set_audio_led(bool on) {
#if ENABLE_STATUS_LEDS
    digitalWrite(LED_AUDIO_PIN, on ? HIGH : LOW);
#else
    (void)on;
#endif
}

static inline void set_link_led(bool on) {
#if ENABLE_STATUS_LEDS
    digitalWrite(LED_LINK_PIN, on ? HIGH : LOW);
#else
    (void)on;
#endif
}

static inline bool is_valid_mode_byte(uint8_t mode) {
    return mode == MODE_AUDIO || mode == MODE_FACE || mode == MODE_BOTH;
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
    return s_last_valid_mode;
}

/**
 * Build a framed packet into buf[].
 * Returns total number of bytes written (including sync, type, len, data, checksum).
 */
static size_t build_packet(uint8_t *buf, uint8_t type,
                            const uint8_t *data, uint16_t data_len) {
    size_t i = 0;
    buf[i++] = SYNC_BYTE_1;
    buf[i++] = SYNC_BYTE_2;
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

/**
 * Thread-safe UART write for small control packets (payload ≤ 8 bytes).
 * Uses a stack-local buffer so the shared audio buffer is not needed.
 */
static void send_control_packet(uint8_t type, const uint8_t *data, uint16_t data_len) {
    // 2 + 1 + 2 + 8 + 1 = 14 bytes maximum
    uint8_t buf[16];
    if (data_len > 8) return; // guard; caller should not exceed this
    size_t pkt_len = build_packet(buf, type, data, data_len);
    xSemaphoreTake(s_uart_tx_mutex, portMAX_DELAY);
    UART0.write(buf, pkt_len);
    xSemaphoreGive(s_uart_tx_mutex);
}

static void set_mode(uint8_t new_mode) {
    if (new_mode == s_mode) return;
    s_mode = new_mode;
    uint8_t payload = new_mode;
    send_control_packet(PKT_MODE_CHANGE, &payload, 1);
}

static void apply_effective_mode(void) {
    uint8_t effective_mode = s_mode_override_active ? s_mode_override : s_switch_mode;
    set_mode(effective_mode);
}

// ─── LED State Machine (called from Supervisor every tick) ───────────────────

static void led_tick(uint32_t now_ms) {
#if !ENABLE_STATUS_LEDS
    (void)now_ms;
    return;
#endif

    static bool     l1 = false, l2 = false;
    static uint32_t l1_last = 0, l2_last = 0;

    // LED1
    switch (s_led1) {
        case LED_OFF:        l1 = false; break;
        case LED_SOLID:      l1 = true;  break;
        case LED_BLINK_1_3S: l1 = ((now_ms % 3000) < 100); break;
        case LED_SLOW_1HZ:
            if (now_ms - l1_last >= 500) { l1 = !l1; l1_last = now_ms; } break;
        case LED_FAST_4HZ:
        case LED_ALT_4HZ:
            if (now_ms - l1_last >= 125) { l1 = !l1; l1_last = now_ms; } break;
    }

    // LED2
    switch (s_led2) {
        case LED_OFF:        l2 = false; break;
        case LED_SOLID:      l2 = true;  break;
        case LED_BLINK_1_3S: l2 = ((now_ms % 3000) < 100); break;
        case LED_SLOW_1HZ:
            if (now_ms - l2_last >= 500) { l2 = !l2; l2_last = now_ms; } break;
        case LED_FAST_4HZ:
            if (now_ms - l2_last >= 125) { l2 = !l2; l2_last = now_ms; } break;
        case LED_ALT_4HZ:
            // Anti-phase to LED1
            l2 = !l1; break;
    }

    set_audio_led(l1);
    set_link_led(l2);
}

/** Derive LED patterns from current system state (called each Supervisor tick). */
static void update_led_patterns(uint32_t now_ms) {
    // Combined catastrophic state: I2S failed AND Pi critical
    if (s_i2s_failed && s_pi_critical) {
        s_led1 = LED_ALT_4HZ;
        s_led2 = LED_ALT_4HZ;
        return;
    }

    // LED1 — Audio / hardware  (highest priority first)
    if (s_i2s_failed) {
        s_led1 = LED_FAST_4HZ;
    } else if (s_mic_dead) {
        s_led1 = LED_SOLID;
    } else if (s_i2s_error && (now_ms - s_i2s_err_ms) < 5000) {
        s_led1 = LED_BLINK_1_3S;  // show for 5 s after last recoverable error
    } else {
        s_led1 = LED_OFF;
    }

    // LED2 — Link / Pi health  (highest priority first)
    if (s_uart_framing || s_pi_critical) {
        s_led2 = LED_SOLID;
    } else if (s_uart_overflow) {
        s_led2 = LED_FAST_4HZ;
    } else if (s_hb_last_ms != 0 && (now_ms - s_hb_last_ms) > HEARTBEAT_TIMEOUT_MS) {
        s_led2 = LED_SLOW_1HZ;
    } else if (s_mode_invalid) {
        s_led2 = LED_BLINK_1_3S;
    } else {
        s_led2 = LED_OFF;
    }
}

// ─── Task: AudioTX (Core 1, priority 5) ──────────────────────────────────────
static void task_audio_tx(void *) {
    static uint32_t zero_since = 0;
    static bool     in_zero   = false;

    while (true) {
        // In FACE-only mode or after hard I2S failure, yield instead of streaming.
        if (s_i2s_failed || s_mode == MODE_FACE) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        size_t bytes_read = 0;
        esp_err_t rc = i2s_read(I2S_PORT, s_i2s_raw, sizeof(s_i2s_raw),
                                 &bytes_read, pdMS_TO_TICKS(100));

        if (rc != ESP_OK || bytes_read == 0) {
            s_i2s_error  = true;
            s_i2s_err_ms = millis();
            continue;
        }

        size_t samples = bytes_read / sizeof(int32_t);
        bool all_zero = true;
        for (size_t i = 0; i < samples && i < BUFFER_SIZE; i++) {
            s_pcm[i] = (int16_t)(s_i2s_raw[i] >> SAMPLE_SHIFT);
            if (s_pcm[i] != 0) all_zero = false;
        }

        // Track microphone silence
        uint32_t now = millis();
        if (all_zero) {
            if (!in_zero) { in_zero = true; zero_since = now; }
            s_mic_dead = ((now - zero_since) > ZERO_SAMPLE_MS);
        } else {
            in_zero    = false;
            s_mic_dead = false;
        }

        // Monitor TX headroom
        s_uart_overflow = (UART0.availableForWrite() < UART_TX_LOW_WATER);

        // Assemble and send audio packet (TYPE = 0x01)
        uint16_t audio_bytes = (uint16_t)(samples * sizeof(int16_t));
        xSemaphoreTake(s_uart_tx_mutex, portMAX_DELAY);
        size_t pkt_len = build_packet(s_audio_pkt, PKT_AUDIO,
                                      (const uint8_t *)s_pcm, audio_bytes);
        UART0.write(s_audio_pkt, pkt_len);
        xSemaphoreGive(s_uart_tx_mutex);
    }
}

// ─── Task: UART_RX (Core 1, priority 4) ──────────────────────────────────────
static void task_uart_rx(void *) {
    static uint8_t  rbuf[256];
    static size_t   rlen = 0;

    while (true) {
        // Drain available bytes into our local ring buffer
        while (UART0.available() && rlen < sizeof(rbuf)) {
            rbuf[rlen++] = (uint8_t)UART0.read();
        }
        if (UART0.available() && rlen >= sizeof(rbuf)) {
            // Overflow: drop oldest byte and flag framing error
            memmove(rbuf, rbuf + 1, sizeof(rbuf) - 1);
            rbuf[sizeof(rbuf) - 1] = (uint8_t)UART0.read();
            s_uart_framing = true;
        }

        // Attempt to parse one or more complete packets
        while (rlen >= 6) {  // minimum: sync(2)+type(1)+len(2)+cs(1)
            // Locate sync pattern
            size_t sync_at = SIZE_MAX;
            for (size_t i = 0; i + 1 < rlen; i++) {
                if (rbuf[i] == SYNC_BYTE_1 && rbuf[i + 1] == SYNC_BYTE_2) {
                    sync_at = i;
                    break;
                }
            }

            if (sync_at == SIZE_MAX) {
                // No sync; keep last byte in case it's the start of a sync pair
                rbuf[0] = rbuf[rlen - 1];
                rlen = 1;
                break;
            }

            if (sync_at > 0) {
                // Discard garbage bytes before sync
                memmove(rbuf, rbuf + sync_at, rlen - sync_at);
                rlen -= sync_at;
                continue;
            }

            // Sync at position 0 — parse header
            uint8_t  pkt_type = rbuf[2];
            uint16_t pkt_len  = (uint16_t)rbuf[3] | ((uint16_t)rbuf[4] << 8);

            if (pkt_len > 128) {
                // Implausibly large — framing error; skip this sync
                s_uart_framing = true;
                memmove(rbuf, rbuf + 2, rlen - 2);
                rlen -= 2;
                continue;
            }

            size_t full = 2u + 1u + 2u + pkt_len + 1u; // sync+type+len+data+cs
            if (rlen < full) break;  // wait for more data

            // Validate checksum
            uint8_t *data_ptr = rbuf + 5;
            uint8_t  expected_cs = 0;
            for (uint16_t i = 0; i < pkt_len; i++) expected_cs += data_ptr[i];
            uint8_t actual_cs = rbuf[5 + pkt_len];

            if (expected_cs != actual_cs) {
                s_uart_framing = true;
                memmove(rbuf, rbuf + 2, rlen - 2);
                rlen -= 2;
                continue;
            }

            // Valid packet — clear framing error
            s_uart_framing = false;

            switch (pkt_type) {
                case PKT_HEARTBEAT:
                    s_hb_last_ms = millis();
                    break;

                case PKT_FORCE_MODE:
                    if (pkt_len >= 1 && is_valid_mode_byte(data_ptr[0])) {
                        s_mode_override = data_ptr[0];
                        s_mode_override_active = true;
                        apply_effective_mode();
                    }
                    break;

                case PKT_CLEAR_MODE:
                    s_mode_override_active = false;
                    apply_effective_mode();
                    break;

                case PKT_PI_STATUS:
                    if (pkt_len >= 1) {
                        s_pi_critical = (data_ptr[0] != 0x00);
                    }
                    break;

                case PKT_ACK:
                    // ACK for our DIAG_REQUEST: signal Supervisor to flash LEDs
                    if (pkt_len >= 1 && data_ptr[0] == PKT_DIAG_REQUEST) {
                        s_diag_ack_pending = true;
                    }
                    break;

                default:
                    break;
            }

            // Consume this packet
            memmove(rbuf, rbuf + full, rlen - full);
            rlen -= full;
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// ─── Task: Supervisor (Core 0, priority 3) ────────────────────────────────────
static void task_supervisor(void *) {
    // Button state machine
    bool     btn_stable    = true;  // HIGH = released (INPUT_PULLUP)
    uint32_t btn_chg_ms    = 0;     // millis() of last stable transition
    uint32_t btn_press_ms  = 0;     // millis() when press began
    bool     btn_long_done = false; // long-press payload already sent this press
    bool     btn_short_pending = false;
    uint32_t btn_short_ms      = 0;

    bool     sw_have_valid_pos = false;
    bool     sw_last_a         = false;
    bool     sw_last_b         = false;

    uint32_t last_tick = millis();

    while (true) {
        uint32_t now = millis();
        if ((now - last_tick) < SUPERVISOR_TICK_MS) {
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }
        last_tick = now;

        // ── Diagnostic ACK flash (from UART_RX signal) ────────────────────────
        if (s_diag_ack_pending) {
            s_diag_ack_pending = false;
#if ENABLE_STATUS_LEDS
            // 3× alternating LED flash to confirm DIAG_REQUEST was acknowledged
            for (int f = 0; f < 3; f++) {
                set_audio_led(true);
                set_link_led(false);
                vTaskDelay(pdMS_TO_TICKS(150));
                set_audio_led(false);
                set_link_led(true);
                vTaskDelay(pdMS_TO_TICKS(150));
            }
            set_audio_led(false);
            set_link_led(false);
#endif
            last_tick = millis();  // reset tick after blocking flash
            continue;
        }

        // ── Mode Switch ──────────────────────────────────────────────────────
#if ENABLE_MODE_SWITCH
        bool pin_a = (digitalRead(MODE_PIN_A) == HIGH);
        bool pin_b = (digitalRead(MODE_PIN_B) == HIGH);

        bool    new_invalid;
        uint8_t new_mode = resolve_switch_mode(pin_a, pin_b, &new_invalid);

        s_mode_invalid = new_invalid;
        if (!new_invalid) {
            if (!sw_have_valid_pos) {
                sw_have_valid_pos = true;
                sw_last_a = pin_a;
                sw_last_b = pin_b;
                s_last_valid_mode = new_mode;
                s_switch_mode = new_mode;
                if (!s_mode_override_active) {
                    apply_effective_mode();
                }
            } else if (pin_a != sw_last_a || pin_b != sw_last_b) {
                sw_last_a = pin_a;
                sw_last_b = pin_b;
                s_last_valid_mode = new_mode;
                s_switch_mode = new_mode;
                if (!s_mode_override_active) {
                    apply_effective_mode();
                }
            }
        }
#else
        s_mode_invalid    = false;
        s_switch_mode     = MODE_BOTH;
        s_last_valid_mode = MODE_BOTH;
        if (!s_mode_override_active) {
            apply_effective_mode();
        }
#endif

        if (s_mode_override_active && s_hb_last_ms != 0 && (now - s_hb_last_ms) > HEARTBEAT_TIMEOUT_MS) {
            s_mode_override_active = false;
            apply_effective_mode();
        }

        // ── Button Debounce ──────────────────────────────────────────────────
#if ENABLE_MARKER_BUTTON
        bool btn_raw = (digitalRead(BUTTON_PIN) == HIGH);  // HIGH = not pressed
        if (btn_raw != btn_stable) {
            if ((now - btn_chg_ms) >= BUTTON_DEBOUNCE_MS) {
                btn_stable = btn_raw;
                btn_chg_ms = now;
                if (!btn_stable) {
                    // Falling edge — button pressed
                    btn_press_ms  = now;
                    btn_long_done = false;
                } else {
                    // Rising edge — button released
                    uint32_t held = now - btn_press_ms;
                    if (!btn_long_done && held >= BUTTON_DEBOUNCE_MS) {
                        if (btn_short_pending && (now - btn_short_ms) <= BUTTON_DOUBLE_PRESS_MS) {
                            btn_short_pending = false;
                            set_mode(MODE_BOTH);
                        } else {
                            btn_short_pending = true;
                            btn_short_ms = now;
                        }
                    }
                }
            }
        } else {
            btn_chg_ms = now;
        }

        if (btn_short_pending && (now - btn_short_ms) > BUTTON_DOUBLE_PRESS_MS) {
            btn_short_pending = false;
            send_control_packet(PKT_MARKER, nullptr, 0);
        }

        // Long-press: fire once while held ≥ threshold
        if (!btn_stable && !btn_long_done &&
            (now - btn_press_ms) >= BUTTON_LONG_PRESS_MS) {
            btn_long_done = true;
            if (btn_short_pending) {
                btn_short_pending = false;
                send_control_packet(PKT_MARKER, nullptr, 0);
            }
            send_control_packet(PKT_DIAG_REQUEST, nullptr, 0);
        }
#endif

        // ── LED Pattern Update ───────────────────────────────────────────────
        update_led_patterns(now);
        led_tick(now);
    }
}

// ─── I2S Initialisation ──────────────────────────────────────────────────────
static bool init_i2s() {
    i2s_config_t cfg = {
        .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate          = I2S_SAMPLE_RATE,
        .bits_per_sample      = I2S_BITS_PER_SAMPLE,
        .channel_format       = I2S_CHANNEL_FORMAT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count        = DMA_BUFFER_COUNT,
        .dma_buf_len          = DMA_BUFFER_SIZE,
        .use_apll             = false,
        .tx_desc_auto_clear   = false,
        .fixed_mclk           = 0,
    };
    i2s_pin_config_t pins = {
        .bck_io_num   = I2S_SCK_PIN,
        .ws_io_num    = I2S_WS_PIN,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = I2S_SD_PIN,
    };

    if (i2s_driver_install(I2S_PORT, &cfg, 0, NULL) != ESP_OK)  return false;
    if (i2s_set_pin(I2S_PORT, &pins)               != ESP_OK) {
        i2s_driver_uninstall(I2S_PORT);
        return false;
    }
    i2s_zero_dma_buffer(I2S_PORT);
    delay(100);
    return true;
}

// ─── setup() ─────────────────────────────────────────────────────────────────
void setup() {
    // GPIO
#if ENABLE_STATUS_LEDS
    pinMode(LED_AUDIO_PIN, OUTPUT);
    pinMode(LED_LINK_PIN,  OUTPUT);
#endif
#if ENABLE_MARKER_BUTTON
    pinMode(BUTTON_PIN,    INPUT_PULLUP);
#endif
    // Test profile uses internal pull-ups on GPIO 22/23. Production keeps its
    // board-defined switch pins and external pull network.
#if ENABLE_MODE_SWITCH
    pinMode(MODE_PIN_A, MODE_PIN_MODE);
    pinMode(MODE_PIN_B, MODE_PIN_MODE);
#endif
    set_audio_led(false);
    set_link_led(false);

    // UART
    UART0.begin(UART_BAUD_RATE);
    delay(50);

    // Watchdog / panic reset indicator — both LEDs solid for 2 s
    esp_reset_reason_t rst = esp_reset_reason();
#if ENABLE_STATUS_LEDS
    if (rst == ESP_RST_WDT      || rst == ESP_RST_PANIC  ||
        rst == ESP_RST_INT_WDT  || rst == ESP_RST_TASK_WDT) {
        set_audio_led(true);
        set_link_led(true);
        delay(2000);
        set_audio_led(false);
        set_link_led(false);
    }
#else
    (void)rst;
#endif

    // Mutex for serialising UART writes from multiple tasks
    s_uart_tx_mutex = xSemaphoreCreateMutex();

    // Read initial mode from switch before spawning tasks
#if ENABLE_MODE_SWITCH
    bool pa = (digitalRead(MODE_PIN_A) == HIGH);
    bool pb = (digitalRead(MODE_PIN_B) == HIGH);
    bool initial_invalid;
    uint8_t initial_mode = resolve_switch_mode(pa, pb, &initial_invalid);
    s_mode_invalid     = initial_invalid;
    s_switch_mode      = initial_mode;
    s_mode             = initial_mode;
    s_last_valid_mode  = initial_mode;
#else
    s_mode_invalid     = false;
    s_switch_mode      = MODE_BOTH;
    s_mode             = MODE_BOTH;
    s_last_valid_mode  = MODE_BOTH;
#endif

    // Seed heartbeat timer (avoid spurious timeout immediately on boot)
    s_hb_last_ms = millis();

    // I2S init
    if (!init_i2s()) {
        s_i2s_failed = true;
        // Supervisor will show LED_FAST_4HZ on LED1. Tasks still run so the
        // Pi link stays active.
    }

    // Spawn FreeRTOS tasks
    // AudioTX and UART_RX share Core 1 (leaves Core 0 for Supervisor + system)
    xTaskCreatePinnedToCore(task_audio_tx,   "AudioTX",    4096, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(task_uart_rx,    "UART_RX",    2048, NULL, 4, NULL, 1);
    xTaskCreatePinnedToCore(task_supervisor, "Supervisor", 2048, NULL, 3, NULL, 0);
}

// ─── loop() ──────────────────────────────────────────────────────────────────
void loop() {
    // All work is handled by FreeRTOS tasks above.
    vTaskDelay(portMAX_DELAY);
}
