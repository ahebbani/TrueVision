from __future__ import annotations

import platform

import cv2

try:
    from picamera2 import Picamera2
except Exception:
    Picamera2 = None  # type: ignore


def _normalize_bgr(arr):
    """Return a BGR frame regardless of backend-native channel layout."""
    if arr is None:
        return None
    if len(arr.shape) == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    if arr.shape[2] == 4:
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    return arr


class PiCam2Capture:
    def __init__(self, width: int, height: int):
        if Picamera2 is None:
            raise RuntimeError("Picamera2 is not available")
        self._picam2 = Picamera2()
        # Ask libcamera for native BGR frames so OpenCV display and dlib's
        # BGR->RGB conversion stay consistent on Raspberry Pi.
        config = self._picam2.create_preview_configuration(
            main={"size": (width, height), "format": "BGR888"}
        )
        self._picam2.configure(config)
        self._picam2.start()

    def read(self):
        arr = self._picam2.capture_array()
        frame = _normalize_bgr(arr)
        return frame is not None, frame

    def isOpened(self):
        return True

    def release(self):
        self._picam2.stop()
        try:
            self._picam2.close()
        except Exception:
            pass


class OpenCVCapture:
    def __init__(self, width: int, height: int, fps: int = 30, camera_index: int = 0):
        system = platform.system()
        backend = cv2.CAP_AVFOUNDATION if system == 'Darwin' else cv2.CAP_ANY
        self._cap = cv2.VideoCapture(camera_index, backend)
        if not self._cap.isOpened() and backend != cv2.CAP_ANY:
            self._cap = cv2.VideoCapture(camera_index)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open webcam index {camera_index}")

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap.set(cv2.CAP_PROP_FPS, fps)

    def read(self):
        ok, frame = self._cap.read()
        if not ok:
            return False, None
        frame = _normalize_bgr(frame)
        return frame is not None, frame

    def isOpened(self):
        return self._cap.isOpened()

    def release(self):
        self._cap.release()


def open_camera(preferred_width: int = 640, preferred_height: int = 480, camera_fps: int = 30, **_kwargs):
    """Open an appropriate camera backend for the current device."""
    system = platform.system()
    machine = platform.machine().lower()
    prefer_picamera = system == 'Linux' and ('arm' in machine or 'aarch64' in machine) and Picamera2 is not None

    if prefer_picamera:
        try:
            print("Camera: Using Picamera2")
            return PiCam2Capture(preferred_width, preferred_height)
        except Exception as exc:
            print(f"Camera: Picamera2 unavailable ({exc}); falling back to OpenCV webcam")

    print("Camera: Using OpenCV webcam")
    return OpenCVCapture(preferred_width, preferred_height, fps=camera_fps)
