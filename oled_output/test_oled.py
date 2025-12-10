#!/usr/bin/env python3
"""
Quick test for the SSD1306 OLED integration.

Usage:
  OLED=1 python3 -m oled_output.test_oled

Optional env vars: OLED_I2C_BUS, OLED_I2C_ADDR, OLED_FONT, OLED_FONT_SIZE
"""
import os
import time
from oled_output.oled_display import get_display


def main():
    disp = get_display()
    disp.update_text(["TrueVision", "OLED test", "addr=", os.environ.get("OLED_I2C_ADDR", "0x3C")])
    time.sleep(2)
    for i in range(5, 0, -1):
        disp.update_text(["Countdown", f"{i}…"])
        time.sleep(1)
    disp.clear()


if __name__ == "__main__":
    main()
