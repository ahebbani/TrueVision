from __future__ import annotations

import platform
import cv2


def _try_open_opencv_device(index: int, w: int, h: int, fps: int):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        if platform.system() == 'Darwin':
            cap2 = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
            if cap2.isOpened():
                cap2.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap2.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                cap2.set(cv2.CAP_PROP_FPS, fps)
                ok, _ = cap2.read()
                if ok:
                    print(f"Camera: Opened via OpenCV AVFoundation (device index {index})")
                    return cap2
                cap2.release()
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv2.CAP_PROP_FPS, fps)
    ok, _ = cap.read()
    if ok:
        print(f"Camera: Opened via OpenCV (device index {index})")
        return cap
    cap.release()
    return None


def _try_open_gstreamer_libcamera(w: int, h: int, fps: int):
    pipeline = (
        f"libcamerasrc ! video/x-raw, width={w}, height={h}, framerate={fps}/1, format=RGB "
        f"! videoconvert ! video/x-raw, format=RGB ! appsink"
    )
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        return None

    class GstCameraCapture:
        def __init__(self, base_cap):
            self._cap = base_cap

        def isOpened(self):
            return self._cap.isOpened()

        def read(self):
            ok, frame = self._cap.read()
            if not ok:
                return ok, frame
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            return True, frame

        def release(self):
            try:
                self._cap.release()
            except Exception:
                pass

    ok, _ = cap.read()
    if not ok:
        cap.release()
        return None
    print("Camera: Opened via GStreamer libcamera pipeline")
    return GstCameraCapture(cap)


def _try_open_picamera2(w: int, h: int):
    try:
        from picamera2 import Picamera2

        class PiCam2Capture:
            def __init__(self, width: int, height: int):
                self._picam2 = Picamera2()
                config = self._picam2.create_preview_configuration(
                    main={"size": (width, height), "format": "RGB888"}
                )
                self._picam2.configure(config)
                self._picam2.start()

            def read(self):
                import cv2 as _cv2
                arr = self._picam2.capture_array()  # RGB
                frame = _cv2.cvtColor(arr, _cv2.COLOR_RGB2BGR)
                return True, frame

            def isOpened(self):
                return True

            def release(self):
                self._picam2.stop()
                try:
                    self._picam2.close()
                except Exception:
                    pass

        print("Camera: Using Picamera2 fallback")
        return PiCam2Capture(w, h)
    except Exception:
        return None


def open_camera(backend: str = 'auto', index: int = 0, preferred_width: int = 640, preferred_height: int = 480, preferred_fps: int = 30):
    backend = (backend or 'auto').lower().strip()

    def try_sequence(seq):
        for name in seq:
            if name == 'opencv':
                cap = _try_open_opencv_device(index, preferred_width, preferred_height, preferred_fps)
                if cap is not None:
                    return cap
            elif name == 'gstreamer':
                cap = _try_open_gstreamer_libcamera(preferred_width, preferred_height, preferred_fps)
                if cap is not None:
                    return cap
            elif name == 'picamera2':
                cap = _try_open_picamera2(preferred_width, preferred_height)
                if cap is not None:
                    return cap
        return None

    if backend == 'opencv':
        return try_sequence(['opencv'])
    elif backend == 'gstreamer':
        return try_sequence(['gstreamer'])
    elif backend == 'picamera2':
        return try_sequence(['picamera2'])
    else:
        return try_sequence(['opencv', 'gstreamer', 'picamera2'])
