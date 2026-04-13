from __future__ import annotations

from picamera2 import Picamera2


class PiCam2Capture:
    def __init__(self, width: int, height: int):
        self._picam2 = Picamera2()
        # BGR888 delivers native BGR bytes — no colour conversion needed
        # and avoids the RGB/BGR ambiguity that caused the blue-tint issue
        # on Pi 5 with Trixie.
        config = self._picam2.create_preview_configuration(
            main={"size": (width, height), "format": "BGR888"}
        )
        self._picam2.configure(config)
        self._picam2.start()

    def read(self):
        arr = self._picam2.capture_array()  # already BGR
        return True, arr

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
