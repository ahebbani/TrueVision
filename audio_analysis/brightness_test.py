import screen_brightness_control as sbc
import time

print("Before:", sbc.get_brightness())
sbc.set_brightness(30)
time.sleep(2)
print("After 30:", sbc.get_brightness())
sbc.set_brightness(80)
time.sleep(2)
print("After 80:", sbc.get_brightness())
sbc.set_brightness(50)
time.sleep(2)
print("After 50:", sbc.get_brightness())
sbc.set_brightness(00)
time.sleep(2)
print("After 80:", sbc.get_brightness())
sbc.set_brightness(90)
time.sleep(2)
print("After 90:", sbc.get_brightness())