from __future__ import annotations

import cv2
from picamera2 import Picamera2


class PiCam2Capture:
    def __init__(self, width: int, height: int):
        self._picam2 = Picamera2()
        # Request RGB from Picamera2, then convert explicitly to BGR before
        # returning frames. This keeps the OpenCV/dlib pipeline deterministic
        # across Picamera2/libcamera variants.
        config = self._picam2.create_preview_configuration(
            main={"size": (width, height), "format": "RGB888"}
        )
        self._picam2.configure(config)
        self._picam2.start()

    def read(self):
        arr = self._picam2.capture_array()
        return True, cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    def isOpened(self):
        return True

    def release(self):
        self._picam2.stop()
        try:
            self._picam2.close()
        except Exception:
            pass


def open_camera(preferred_width: int = 640, preferred_height: int = 480, **_kwargs):
    """Open the Raspberry Pi camera via Picamera2."""
    print("Camera: Using Picamera2")
    return PiCam2Capture(preferred_width, preferred_height)
