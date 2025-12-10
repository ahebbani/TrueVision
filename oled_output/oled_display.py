"""
Optional SSD1306 OLED status display for Raspberry Pi.

- Defaults to disabled. Enable by setting environment variable OLED=1
- Works over I2C (address default 0x3C) using luma.oled
- Safe no-op on non-Pi systems or when libraries/hardware are missing

Env vars:
  OLED                 -> '1'/'true' to enable (default: disabled)
  OLED_I2C_BUS         -> I2C bus number (default: 1)
  OLED_I2C_ADDR        -> I2C address in hex (default: 0x3C)
  OLED_WIDTH           -> display width (default: 128)
  OLED_HEIGHT          -> display height (default: 64)
  OLED_FONT            -> path to a .ttf font (optional)
  OLED_FONT_SIZE       -> font size (default: 12)
"""
from __future__ import annotations

import os
import time
from typing import List, Optional


class _DummyDisplay:
    def update_text(self, lines: List[str]) -> None:
        # No-op; prints to console occasionally for visibility
        pass

    def clear(self) -> None:
        pass


class _LumaDisplay:
    def __init__(self, device, font, width: int, height: int):
        self._device = device
        self._font = font
        self._width = width
        self._height = height
        self._last_text = None
        self._last_ts = 0.0

    def update_text(self, lines: List[str]) -> None:
        try:
            from luma.core.render import canvas
        except Exception:
            return
        # Normalize and throttle to avoid redundant updates
        lines = [str(s)[:64] for s in (lines or [])]
        text_blob = "\n".join(lines)
        now = time.time()
        if text_blob == self._last_text and (now - self._last_ts) < 0.25:
            return
        self._last_text = text_blob
        self._last_ts = now
        # Draw
        y = 0
        line_h = max(8, int(getattr(self._font, "size", 12)))
        with canvas(self._device) as draw:
            for line in lines[:8]:
                draw.text((0, y), line, font=self._font, fill=255)
                y += line_h

    def clear(self) -> None:
        self.update_text([""])


def _try_build_luma_display() -> Optional[_LumaDisplay]:
    # Auto-detect: attempt to initialize if libraries and hardware are available.
    try:
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306, sh1106
        from PIL import ImageFont
    except Exception as e:
        print(f"OLED: libraries not available ({e}); continuing without OLED.")
        return None

    width = int(os.environ.get("OLED_WIDTH", "128"))
    height = int(os.environ.get("OLED_HEIGHT", "64"))

    # I2C config
    bus = int(os.environ.get("OLED_I2C_BUS", "1"))
    addr_raw = os.environ.get("OLED_I2C_ADDR", "0x3C")
    try:
        address = int(addr_raw, 16) if isinstance(addr_raw, str) else int(addr_raw)
    except Exception:
        address = 0x3C

    try:
        serial = i2c(port=bus, address=address)
        # Prefer SSD1306, but support SH1106 panels too via env OLED_DRIVER
        driver = os.environ.get("OLED_DRIVER", "ssd1306").lower()
        if driver == "sh1106":
            device = sh1106(serial, width=width, height=height)
        else:
            device = ssd1306(serial, width=width, height=height)
    except Exception as e:
        print(f"OLED: failed to open I2C device ({e}); continuing without OLED.")
        return None

    # Font setup
    font = None
    font_path = os.environ.get("OLED_FONT")
    font_size = int(os.environ.get("OLED_FONT_SIZE", "12"))
    try:
        if font_path and os.path.exists(font_path):
            font = ImageFont.truetype(font_path, font_size)
        else:
            # default bitmap font is compact and crisp
            font = ImageFont.load_default()
    except Exception:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    return _LumaDisplay(device, font, width, height)


def get_display():
    disp = _try_build_luma_display()
    if disp is None:
        return _DummyDisplay()
    return disp
