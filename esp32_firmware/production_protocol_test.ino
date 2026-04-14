/*
 * Production Protocol UART Test
 *
 * Sends framed packets using the exact TrueVision production protocol
 * (sync + type + len + data + checksum) so the Pi can validate packet
 * integrity at different baud rates.
 *
 * Protocol frame:
 *   [0xAA][0x55][TYPE(1)][LEN_LO][LEN_HI][DATA(LEN)][CHECKSUM]
 *   Checksum = sum(data bytes) & 0xFF
 *
 * What it sends:
 *   - PKT_AUDIO (0x01) packets with a 440 Hz sine wave (BUFFER_SIZE samples)
 *   - PKT_MODE_CHANGE (0x02) every 5 seconds, toggling AUDIO/FACE
 *
 * ── USAGE ──────────────────────────────────────────────────────────────────
 * 1. Change TEST_BAUD below to the baud rate you want to test.
 * 2. Flash this sketch to the ESP32.
 * 3. On the Pi, run:
 *      sudo python test_production_protocol.py --baud <same_rate> --seconds 5
 *
 * Test these in order: 230400, 460800, 921600
 * Use the fastest rate that reports PASS.
 *
 * Wiring:
 *   ESP32 TX0 (GPIO 1) → Pi RXD (physical pin 10 / GPIO 15)
 *   ESP32 GND           → Pi GND
 */

// ═══════════════════════════════════════════════════════════════════════════
// CHANGE THIS to test different baud rates: 115200, 230400, 460800, 921600
#define TEST_BAUD       230400
// ═══════════════════════════════════════════════════════════════════════════

#define BUFFER_SIZE     128    // samples per audio packet

// Protocol constants (match production firmware)
#define SYNC_1          0xAA
#define SYNC_2          0x55
#define PKT_AUDIO       0x01
#define PKT_MODE_CHANGE 0x02
#define MODE_AUDIO      0x00
#define MODE_FACE       0x01

static HardwareSerial &UART0 = Serial0;
static int16_t  pcm_buf[BUFFER_SIZE];
static uint8_t  pkt_buf[BUFFER_SIZE * 2 + 6];

static uint32_t sample_counter  = 0;
static uint32_t mode_toggle_ms  = 0;
static uint8_t  current_mode    = MODE_AUDIO;
static uint32_t pkt_count       = 0;

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

void setup() {
    UART0.begin(TEST_BAUD);
    delay(200);
    mode_toggle_ms = millis();
}

void loop() {
    // Generate synthetic 440 Hz sine wave (16 kHz sample rate)
    for (int i = 0; i < BUFFER_SIZE; i++) {
        float t = (float)(sample_counter + i) / 16000.0f;
        pcm_buf[i] = (int16_t)(16000.0f * sinf(2.0f * 3.14159265f * 440.0f * t));
    }
    sample_counter += BUFFER_SIZE;

    // Build and send audio packet
    uint16_t audio_bytes = (uint16_t)(BUFFER_SIZE * sizeof(int16_t));
    size_t pkt_len = build_packet(pkt_buf, PKT_AUDIO,
                                  (const uint8_t *)pcm_buf, audio_bytes);
    UART0.write(pkt_buf, pkt_len);
    pkt_count++;

    // Toggle mode every 5 seconds
    if (millis() - mode_toggle_ms > 5000) {
        current_mode = (current_mode == MODE_AUDIO) ? MODE_FACE : MODE_AUDIO;
        uint8_t payload = current_mode;
        uint8_t mode_pkt[8];
        size_t mlen = build_packet(mode_pkt, PKT_MODE_CHANGE, &payload, 1);
        UART0.write(mode_pkt, mlen);
        mode_toggle_ms = millis();
    }

    // Pace output so we don't overflow the UART TX buffer.
    // Usable throughput ≈ baud / 10 bytes/sec (8N1).
    uint32_t usable_bps  = TEST_BAUD / 10;
    uint32_t pkt_bytes   = audio_bytes + 6;
    uint32_t delay_ms    = (pkt_bytes * 1000UL) / usable_bps;
    if (delay_ms < 1) delay_ms = 1;
    delay(delay_ms + 1);  // +1 ms margin
}
