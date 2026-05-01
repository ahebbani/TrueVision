#include <Arduino.h>
#include <HardwareSerial.h>
#include <driver/i2s.h>

namespace {
constexpr uint8_t FRAME_A = 0xAA;
constexpr uint8_t FRAME_B = 0x55;
constexpr uint8_t TYPE_AUDIO = 0x01;
constexpr uint8_t TYPE_MODE = 0x02;
constexpr uint8_t TYPE_MARKER = 0x03;

constexpr int HEARTBEAT_LED = 9;
constexpr int PACKET_LED = 10;
constexpr int RECORD_LED = 38;
constexpr int MODE_AUDIO_PIN = 35;
constexpr int MODE_FACE_PIN = 36;

constexpr int I2S_SCK = 8;
constexpr int I2S_WS = 6;
constexpr int I2S_SD = 7;
constexpr int UART_TX = 43;
constexpr int UART_RX = 44;

constexpr uint32_t SAMPLE_RATE = 16000;
constexpr size_t PACKET_SAMPLES = 256;
constexpr uint32_t UART_BAUD = 921600;
constexpr uint32_t HEARTBEAT_MS = 500;
constexpr uint32_t DEBOUNCE_MS = 50;

HardwareSerial SerialPi(0);
int32_t i2sBuffer[PACKET_SAMPLES];
int16_t pcmBuffer[PACKET_SAMPLES];

enum DeviceMode : uint8_t {
  AUDIO_MODE = 0x00,
  FACE_MODE = 0x01,
};

DeviceMode currentMode = FACE_MODE;
DeviceMode stableMode = FACE_MODE;
uint32_t lastHeartbeatAt = 0;
uint32_t lastModeChangeAt = 0;
bool heartbeatState = false;

uint8_t checksum(const uint8_t* data, size_t length) {
  uint32_t sum = 0;
  for (size_t index = 0; index < length; ++index) {
    sum += data[index];
  }
  return static_cast<uint8_t>(sum & 0xFF);
}

void sendFrame(uint8_t type, const uint8_t* payload, uint16_t length) {
  SerialPi.write(FRAME_A);
  SerialPi.write(FRAME_B);
  SerialPi.write(type);
  SerialPi.write(static_cast<uint8_t>(length & 0xFF));
  SerialPi.write(static_cast<uint8_t>((length >> 8) & 0xFF));
  if (length > 0) {
    SerialPi.write(payload, length);
  }
  SerialPi.write(checksum(payload, length));
}

void pulsePacketLed() {
  digitalWrite(PACKET_LED, HIGH);
  delayMicroseconds(400);
  digitalWrite(PACKET_LED, LOW);
}

DeviceMode readSwitchMode() {
  const int audioState = digitalRead(MODE_AUDIO_PIN);
  const int faceState = digitalRead(MODE_FACE_PIN);
  if (audioState == HIGH && faceState == LOW) {
    return AUDIO_MODE;
  }
  return FACE_MODE;
}

void sendCurrentMode() {
  const uint8_t payload[1] = { static_cast<uint8_t>(currentMode) };
  sendFrame(TYPE_MODE, payload, 1);
}

void configureI2S() {
  const i2s_config_t i2sConfig = {
    .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 4,
    .dma_buf_len = PACKET_SAMPLES,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0,
  };

  const i2s_pin_config_t pinConfig = {
    .bck_io_num = I2S_SCK,
    .ws_io_num = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD,
  };

  i2s_driver_install(I2S_NUM_0, &i2sConfig, 0, nullptr);
  i2s_set_pin(I2S_NUM_0, &pinConfig);
}

void readAndSendAudio() {
  size_t bytesRead = 0;
  if (i2s_read(I2S_NUM_0, i2sBuffer, sizeof(i2sBuffer), &bytesRead, 0) != ESP_OK || bytesRead == 0) {
    return;
  }

  const size_t sampleCount = bytesRead / sizeof(int32_t);
  for (size_t index = 0; index < sampleCount; ++index) {
    pcmBuffer[index] = static_cast<int16_t>(i2sBuffer[index] >> 14);
  }

  sendFrame(TYPE_AUDIO, reinterpret_cast<uint8_t*>(pcmBuffer), static_cast<uint16_t>(sampleCount * sizeof(int16_t)));
  pulsePacketLed();
}

void updateModeSwitch() {
  const DeviceMode observed = readSwitchMode();
  if (observed != stableMode) {
    lastModeChangeAt = millis();
    stableMode = observed;
  }
  if (stableMode != currentMode && millis() - lastModeChangeAt >= DEBOUNCE_MS) {
    currentMode = stableMode;
    sendCurrentMode();
  }
}

void updateHeartbeat() {
  const uint32_t now = millis();
  if (now - lastHeartbeatAt < HEARTBEAT_MS) {
    return;
  }
  lastHeartbeatAt = now;
  heartbeatState = !heartbeatState;
  digitalWrite(HEARTBEAT_LED, heartbeatState ? HIGH : LOW);
}
}

void setup() {
  pinMode(HEARTBEAT_LED, OUTPUT);
  pinMode(PACKET_LED, OUTPUT);
  pinMode(MODE_AUDIO_PIN, INPUT);
  pinMode(MODE_FACE_PIN, INPUT);

  SerialPi.begin(UART_BAUD, SERIAL_8N1, UART_RX, UART_TX);
  configureI2S();

  currentMode = readSwitchMode();
  stableMode = currentMode;
  sendCurrentMode();
}

void loop() {
  updateHeartbeat();
  updateModeSwitch();
  readAndSendAudio();
}
