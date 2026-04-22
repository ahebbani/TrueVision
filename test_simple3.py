import serial

ser = serial.Serial('/dev/serial0', 115200, timeout=0.5)

print("Listening...")

while True:
    line = ser.readline().decode(errors='ignore').strip()
    if line:
        print("From ESP32:", line)