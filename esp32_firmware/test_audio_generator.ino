/*
 * ESP32 Test Audio Generator - Simulates I2S Microphone
 * 
 * Generates test audio tones and streams them to Raspberry Pi via UART.
 * Use this to test the serial audio pipeline without a physical microphone.
 * 
 * Hardware Requirements:
 * - ESP32 Dev Board
 * - 2 wires to Raspberry Pi for one-way testing (TX, GND)
 * - Optional 3rd signal wire (Pi TX -> ESP32 RX) if you want bidirectional tests later
 * 
 * UART to Raspberry Pi:
 * - ESP32 TX (GPIO 1)  -> Pi RX (Physical Pin 10, GPIO 15)
 * - ESP32 RX (GPIO 3)  -> Pi TX (Physical Pin 8, GPIO 14)
 * - ESP32 GND          -> Pi GND (Physical Pin 6)
 * 
 * Test Modes:
 * 1. Sine wave tone (440 Hz A note)
 * 2. Sweep tone (200-800 Hz)
 * 3. Speech-like synthetic pattern (varying frequencies and amplitudes)
 * 
 * Change TEST_MODE below to select different test signals.
 *
 * Notes:
 * - This sketch is for UART framing and audio-path validation, not speech recognition validation.
 * - Keep the Arduino Serial Monitor closed while streaming to the Raspberry Pi because UART0
 *   is shared with the packet stream.
 * 
 * Author: TrueVision Project
 * Date: January 2026
 */

#include <math.h>

// Audio Configuration (must match receiver settings)
#define SAMPLE_RATE       16000
#define BUFFER_SIZE       512  // Samples per packet (32ms at 16kHz)

// UART Configuration
#define UART_BAUD_RATE    921600

// Protocol bytes
#define SYNC_BYTE_1       0xAA
#define SYNC_BYTE_2       0x55
#define PKT_AUDIO         0x01

// Test mode selection
#define TEST_MODE_SINE    1  // Constant 440 Hz tone
#define TEST_MODE_SWEEP   2  // Frequency sweep 200-800 Hz
#define TEST_MODE_SPEECH  3  // Simulated speech pattern

#define TEST_MODE         TEST_MODE_SPEECH  // Change this to try different modes

// Buffers
int16_t audio_buffer[BUFFER_SIZE];
uint8_t uart_packet[BUFFER_SIZE * 2 + 6];

// Test signal state
float phase = 0.0;
float frequency = 440.0;  // Hz
float amplitude = 8000.0;  // ~25% of max int16
unsigned long sample_counter = 0;

void setup() {
  // Initialize UART
  Serial.begin(UART_BAUD_RATE);
  delay(1000);
}

void generate_sine_wave() {
  // Generate a simple sine wave at fixed frequency
  for (int i = 0; i < BUFFER_SIZE; i++) {
    audio_buffer[i] = (int16_t)(amplitude * sin(phase));
    phase += 2.0 * PI * frequency / SAMPLE_RATE;
    if (phase >= 2.0 * PI) {
      phase -= 2.0 * PI;
    }
  }
}

void generate_sweep() {
  // Generate a frequency sweep from 200 to 800 Hz
  for (int i = 0; i < BUFFER_SIZE; i++) {
    // Update frequency every second (16000 samples)
    float sweep_progress = (float)((sample_counter + i) % SAMPLE_RATE) / SAMPLE_RATE;
    frequency = 200.0 + sweep_progress * 600.0;  // 200 to 800 Hz
    
    audio_buffer[i] = (int16_t)(amplitude * sin(phase));
    phase += 2.0 * PI * frequency / SAMPLE_RATE;
    if (phase >= 2.0 * PI) {
      phase -= 2.0 * PI;
    }
  }
}

void generate_speech_pattern() {
  // Simulate speech-like patterns with varying frequency and amplitude.
  // This is useful for waveform/path testing, but it is still synthetic audio,
  // so speech-to-text should not be expected to produce meaningful words.
  
  for (int i = 0; i < BUFFER_SIZE; i++) {
    unsigned long total_samples = sample_counter + i;
    
    // Slow modulation (syllable-like, ~3 Hz)
    float syllable_phase = (float)(total_samples % (SAMPLE_RATE / 3)) / (SAMPLE_RATE / 3);
    
    // Fast modulation (pitch variation, ~150-300 Hz)
    float pitch_variation = sin(2.0 * PI * syllable_phase * 2.0);
    frequency = 200.0 + pitch_variation * 100.0;  // 100-300 Hz range (human voice)
    
    // Amplitude modulation (volume variation)
    float volume_envelope = 0.5 + 0.5 * sin(2.0 * PI * syllable_phase);
    
    // Add some harmonics for richer sound
    float sample = sin(phase);  // Fundamental
    sample += 0.3 * sin(2.0 * phase);  // 2nd harmonic
    sample += 0.15 * sin(3.0 * phase);  // 3rd harmonic
    
    audio_buffer[i] = (int16_t)(amplitude * volume_envelope * sample * 0.7);
    
    phase += 2.0 * PI * frequency / SAMPLE_RATE;
    if (phase >= 2.0 * PI) {
      phase -= 2.0 * PI;
    }
    
    // Add some noise for realism (very small amount)
    audio_buffer[i] += (int16_t)(random(-50, 50));
  }
}

void loop() {
  // Generate test audio based on selected mode
  switch (TEST_MODE) {
    case TEST_MODE_SINE:
      generate_sine_wave();
      break;
    case TEST_MODE_SWEEP:
      generate_sweep();
      break;
    case TEST_MODE_SPEECH:
      generate_speech_pattern();
      break;
    default:
      generate_sine_wave();
  }
  
  sample_counter += BUFFER_SIZE;
  
  // Build UART packet using the current TrueVision framed protocol:
  // [SYNC][TYPE][LEN_LO][LEN_HI][DATA][CHECKSUM]
  size_t audio_bytes = BUFFER_SIZE * sizeof(int16_t);
  size_t packet_idx = 0;
  
  // Sync bytes
  uart_packet[packet_idx++] = SYNC_BYTE_1;
  uart_packet[packet_idx++] = SYNC_BYTE_2;
  uart_packet[packet_idx++] = PKT_AUDIO;
  
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
  
  // Small delay to maintain proper timing (32ms per packet at 16kHz)
  // This ensures we stream at real-time rate
  delay(32);
}
