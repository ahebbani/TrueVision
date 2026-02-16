// ESP32 + SPH0645 I2S mic -> UDP audio stream
// Packet format: [4-byte "AUD0"][2-byte payload_len LE][PCM16LE payload]
// Payload: 256 samples (mono) @ 16kHz => 512 bytes per packet

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "driver/i2s.h"

// ---------------- Wi-Fi / UDP ----------------
const char* WIFI_SSID = "ZOHAIBSINTERNET";
const char* WIFI_PASS = "Zohaibisbest";

// IMPORTANT: update if your laptop IP changes
IPAddress laptopIP(172, 20, 10, 3);
const uint16_t laptopPort = 5005;

WiFiUDP udp;

// ---------------- Audio format ----------------
static const uint32_t SAMPLE_RATE = 16000;
static const uint16_t SAMPLES_PER_CHUNK = 256;              // 16 ms per packet
static const uint16_t PAYLOAD_LEN = SAMPLES_PER_CHUNK * 2;  // PCM16 bytes
static const uint8_t  MAGIC[4] = {'A','U','D','0'};

// Packet buffer
static uint8_t pkt[4 + 2 + PAYLOAD_LEN];
static int16_t pcm[SAMPLES_PER_CHUNK];

// ---------------- I2S pins (SPH0645) ----------------
// SPH0645: BCLK(SCK), LRCLK(WS), DOUT(SD)
// SEL tied to GND => LEFT channel
static const int I2S_WS  = 25;   // LRCLK / WS
static const int I2S_SCK = 26;   // BCLK / SCK
static const int I2S_SD  = 33;   // DOUT from mic -> ESP32 data_in

static const i2s_port_t I2S_PORT = I2S_NUM_0;

// Timing (precise 16ms pacing)
static uint32_t next_us = 0;

static void setup_i2s() {
  // SPH0645 outputs 24-bit audio in 32-bit words.
  i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = (int)SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,          // SEL=GND -> LEFT
    .communication_format = I2S_COMM_FORMAT_I2S,          // if noisy, try I2S_MSB (see note below)
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };

  i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_SCK,
    .ws_io_num = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD
  };

  ESP_ERROR_CHECK(i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL));
  ESP_ERROR_CHECK(i2s_set_pin(I2S_PORT, &pin_config));
  ESP_ERROR_CHECK(i2s_zero_dma_buffer(I2S_PORT));
}

void setup() {
  Serial.begin(115200);
  delay(200);

  // Wi-Fi connect
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("ESP32 IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("Sending UDP to: ");
  Serial.print(laptopIP);
  Serial.print(":");
  Serial.println(laptopPort);

  // Packet header
  memcpy(pkt, MAGIC, 4);
  pkt[4] = (uint8_t)(PAYLOAD_LEN & 0xFF);
  pkt[5] = (uint8_t)((PAYLOAD_LEN >> 8) & 0xFF);

  // I2S init
  setup_i2s();

  // Prime pacing
  next_us = micros();
}

void loop() {
  // Read 256 samples as 32-bit words
  static int32_t i2s_words[SAMPLES_PER_CHUNK];
  size_t bytes_read = 0;

  esp_err_t err = i2s_read(I2S_PORT, (void*)i2s_words, sizeof(i2s_words), &bytes_read, portMAX_DELAY);
  if (err != ESP_OK || bytes_read != sizeof(i2s_words)) {
    Serial.println("i2s_read failed / short read");
    return;
  }

  // Convert 32-bit mic samples -> PCM16
  // Tune SHIFT if needed:
  // - too quiet => SHIFT 12 or 11
  // - clipping  => SHIFT 14 or 15
  const int SHIFT = 13;

  for (int i = 0; i < SAMPLES_PER_CHUNK; i++) {
    int32_t s = i2s_words[i];
    int32_t v = s >> SHIFT;

    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    pcm[i] = (int16_t)v;
  }

  // Copy PCM into packet and send
  memcpy(pkt + 6, (uint8_t*)pcm, PAYLOAD_LEN);

  udp.beginPacket(laptopIP, laptopPort);
  udp.write(pkt, sizeof(pkt));
  udp.endPacket();

  // Precise pacing: 256/16000 = 0.016s = 16000us
  next_us += 16000;
  int32_t wait_us = (int32_t)(next_us - micros());
  if (wait_us > 0) delayMicroseconds(wait_us);
  else next_us = micros(); // resync if behind
}

/*
Troubleshooting notes:
1) If audio is silence:
   - Try .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT (some boards label SEL opposite)
   - Verify wiring: WS->25, BCLK->26, DOUT->33, SEL->GND, 3V3/GND correct.

2) If audio is noisy/robotic:
   - Try .communication_format = I2S_COMM_FORMAT_I2S_MSB

3) If audio is too quiet or too loud:
   - Adjust SHIFT (12 louder, 14 quieter).

4) If laptop IP changes (common on hotspot):
   - Update laptopIP(...) accordingly.
*/
