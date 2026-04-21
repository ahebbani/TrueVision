import serial
import time

ser = serial.Serial('/dev/serial0', 115200, timeout=0.5)
time.sleep(2)

print("Listening on /dev/serial0 ...")

while True:
    line = ser.readline().decode(errors='ignore').strip()
    if line:
        print("From ESP32:", line)
        ser.write(b"HELLO_FROM_RPI\n")