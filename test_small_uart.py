import serial
ser = serial.Serial('/dev/serial0', 115200, timeout=1)

ser.write(b'Hello from Pi\n')

while True:
    line = ser.readline()
    if line:
        print(line.decode(errors='replace').strip())