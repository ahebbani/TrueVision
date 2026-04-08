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
    # Request BGR output directly from the pipeline so OpenCV receives frames
    # in the format it expects without any further conversion.
    # On Pi 5 + Trixie, libcamerasrc delivers BGR-ordered bytes; requesting
    # format=RGB and then doing COLOR_RGB2BGR double-swaps to wrong colours.
    pipeline = (
        f"libcamerasrc ! video/x-raw, width={w}, height={h}, framerate={fps}/1 "
        f"! videoconvert ! video/x-raw, format=BGR ! appsink"
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
            # Frame arrives as BGR — no conversion needed.
            return self._cap.read()

        def release(self):
            try:
                self._cap.release()
            except Exception:
                pass

    ok, _ = cap.read()
    if not ok:
        cap.release()
        return None
    print("Camera: Opened via GStreamer libcamera pipeline (BGR output)")
    return GstCameraCapture(cap)


def _try_open_picamera2(w: int, h: int):
    try:
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
        # On Linux (Raspberry Pi), prefer GStreamer and Picamera2 over the raw
        # V4L2 OpenCV path. The V4L2/libcamera compat layer on Pi 5 + Trixie
        # can deliver frames in the wrong colour format (RGB instead of BGR),
        # causing a blue tint and dlib detection failures. The GStreamer and
        # Picamera2 paths both explicitly convert to BGR and are reliable.
        if platform.system() == 'Linux':
            return try_sequence(['gstreamer', 'picamera2', 'opencv'])
        return try_sequence(['opencv', 'gstreamer', 'picamera2'])
