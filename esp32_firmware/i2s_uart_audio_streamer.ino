/*
 * ESP32 I2S Microphone to UART Audio Streamer
 * 
 * Captures audio from an I2S microphone and streams it to Raspberry Pi via UART.
 * 
 * Hardware Requirements:
 * - ESP32 Dev Board
 * - I2S MEMS Microphone (e.g., INMP441, ICS-43434, SPH0645)
 * - 3 wires to Raspberry Pi (TX, RX, GND)
 * 
 * I2S Microphone Wiring:
 * - SCK  (Serial Clock)     -> GPIO 14
 * - WS   (Word Select/LRCK) -> GPIO 15
 * - SD   (Serial Data)      -> GPIO 32
 * - VDD  -> 3.3V
 * - GND  -> GND
 * 
 * UART to Raspberry Pi:
 * - ESP32 TX (GPIO 1)  -> Pi RX (Physical Pin 10, GPIO 15)
 * - ESP32 RX (GPIO 3)  -> Pi TX (Physical Pin 8, GPIO 14)
 * - ESP32 GND          -> Pi GND (Physical Pin 6)
 * 
 * Audio Format:
 * - Sample Rate: 16000 Hz
 * - Bit Depth: 16-bit signed PCM
 * - Channels: Mono
 * 
 * Protocol:
 * - [SYNC(0xAA 0x55)] [LENGTH(2 bytes LE)] [AUDIO_DATA] [CHECKSUM(1 byte)]
 * 
 * Author: TrueVision Project
 * Date: January 2026
 */

#include <driver/i2s.h>

// I2S Configuration
#define I2S_PORT          I2S_NUM_0
#define I2S_SAMPLE_RATE   16000
#define I2S_BITS_PER_SAMPLE I2S_BITS_PER_SAMPLE_32BIT  // Many I2S mics output 32-bit
#define I2S_CHANNELS      1  // Mono

// I2S Pins
// NOTE: These defaults match the "free GPIO" mapping.
// Change if needed to match your wiring.
#define I2S_SCK_PIN       16  // Serial Clock (BCLK)
#define I2S_WS_PIN        17  // Word Select (LRCLK/WS)
#define I2S_SD_PIN        5   // Serial Data (DOUT)

// Many I2S mics (including SPH0645) output 24-bit audio in 32-bit frames.
// Adjust if your audio is too quiet/loud: common values are 11, 13, 14, 16.
#define SAMPLE_SHIFT      14

// If your mic has SEL/LR pin: SEL=GND usually outputs LEFT, SEL=3V3 outputs RIGHT.
// Match this to how you wired SEL.
#define I2S_CHANNEL_FORMAT I2S_CHANNEL_FMT_ONLY_LEFT

// Audio Buffer Configuration
#define BUFFER_SIZE       512  // Samples per packet (32ms at 16kHz)
#define DMA_BUFFER_COUNT  4
#define DMA_BUFFER_SIZE   1024

// UART Configuration
#define UART_BAUD_RATE    921600
#define UART_PORT         UART_NUM_0  // Uses GPIO 1 (TX) and GPIO 3 (RX)

// Protocol bytes
#define SYNC_BYTE_1       0xAA
#define SYNC_BYTE_2       0x55

// Buffers
int32_t i2s_read_buffer[BUFFER_SIZE];
int16_t audio_buffer[BUFFER_SIZE];
uint8_t uart_packet[BUFFER_SIZE * 2 + 5];  // Sync(2) + Length(2) + Audio + Checksum(1)

void setup() {
  // Initialize UART
  Serial.begin(UART_BAUD_RATE);
  while (!Serial) {
    delay(10);
  }
  
  Serial.println("ESP32 I2S to UART Audio Streamer");
  Serial.println("Initializing I2S...");
  
  // Configure I2S
  i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = I2S_SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE,
    .channel_format = I2S_CHANNEL_FORMAT,  // Mono - pick left/right to match SEL
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = DMA_BUFFER_COUNT,
    .dma_buf_len = DMA_BUFFER_SIZE,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };
  
  // Pin configuration
  i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_SCK_PIN,
    .ws_io_num = I2S_WS_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD_PIN
  };
  
  // Install and configure I2S driver
  esp_err_t err = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  if (err != ESP_OK) {
    Serial.printf("Failed to install I2S driver: %d\n", err);
    while (1) {
      delay(1000);
    }
  }
  
  err = i2s_set_pin(I2S_PORT, &pin_config);
  if (err != ESP_OK) {
    Serial.printf("Failed to set I2S pins: %d\n", err);
    while (1) {
      delay(1000);
    }
  }
  
  // Clear I2S buffer to remove startup noise
  i2s_zero_dma_buffer(I2S_PORT);
  delay(100);
  
  Serial.println("I2S initialized successfully");
  Serial.println("Starting audio streaming to UART...");
  delay(1000);
}

void loop() {
  // Read audio from I2S microphone
  size_t bytes_read = 0;
  esp_err_t result = i2s_read(I2S_PORT, i2s_read_buffer, sizeof(i2s_read_buffer), 
                               &bytes_read, portMAX_DELAY);
  
  if (result != ESP_OK || bytes_read == 0) {
    return;  // Skip this iteration
  }
  
  size_t samples_read = bytes_read / sizeof(int32_t);
  
  // Convert 32-bit I2S samples to 16-bit PCM
  // Most I2S microphones output in the upper bits of 32-bit words
  for (size_t i = 0; i < samples_read && i < BUFFER_SIZE; i++) {
    // Shift right to get 16-bit data from 32-bit (discard lower bits)
    // Adjust shift amount
    audio_buffer[i] = (int16_t)(i2s_read_buffer[i] >> SAMPLE_SHIFT);
  }
  
  // Build UART packet
  size_t audio_bytes = samples_read * sizeof(int16_t);
  size_t packet_idx = 0;
  
  // Sync bytes
  uart_packet[packet_idx++] = SYNC_BYTE_1;
  uart_packet[packet_idx++] = SYNC_BYTE_2;
  
  // Length (little-endian uint16)
  uart_packet[packet_idx++] = audio_bytes & 0xFF;
  uart_packet[packet_idx++] = (audio_bytes >> 8) & 0xFF;
  
  // Audio data
  uint8_t checksum = 0;
  for (size_t i = 0; i < audio_bytes; i++) {
    uint8_t byte = ((uint8_t*)audio_buffer)[i];
    uart_packet[packet_idx++] = byte;
    checksum += byte;
  }
  
  // Checksum
  uart_packet[packet_idx++] = checksum;
  
  // Send packet over UART
  Serial.write(uart_packet, packet_idx);
  
  // Optional: Flash LED to show activity (if your board has one on GPIO 2)
  // digitalWrite(2, !digitalRead(2));
}
