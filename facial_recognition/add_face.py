import cv2
import dlib
import numpy as np
import os
import time
import platform

# Initialize face detector and shape predictor
detector = dlib.get_frontal_face_detector()

# Resolve paths relative to this file so it works from any CWD
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, 'models')
# Legacy DB_DIR removed; database now centrally managed in data_access.

predictor_path = os.path.join(MODELS_DIR, 'shape_predictor_68_face_landmarks.dat')
predictor = dlib.shape_predictor(predictor_path)

face_rec_model_path = os.path.join(MODELS_DIR, 'dlib_face_recognition_resnet_model_v1.dat')
face_rec_model = dlib.face_recognition_model_v1(face_rec_model_path)

from data_access import open_db

# Reuse centralized DB (schema ensured automatically)
conn = open_db()
cursor = conn.cursor()

CAMERA_BACKEND = os.environ.get('CAMERA_BACKEND', 'auto')  # 'auto' | 'opencv' | 'gstreamer' | 'picamera2'
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))   # used when backend == 'opencv'

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
                arr = self._picam2.capture_array()  # RGB
                frame = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
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


def open_camera(preferred_width: int = 640, preferred_height: int = 480, preferred_fps: int = 30):
    backend = CAMERA_BACKEND.lower().strip()
    def try_sequence(seq):
        for name in seq:
            if name == 'opencv':
                cap = _try_open_opencv_device(CAMERA_INDEX, preferred_width, preferred_height, preferred_fps)
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


def add_face(name):
    cap = open_camera()
    if cap is None:
        print("ERROR: Could not open any camera. On Raspberry Pi, ensure libcamera works (try: libcamera-hello).\n"
              "Install either python3-opencv with GStreamer support, or python3-picamera2.")
        return
    print("Press 's' to capture the face and save it.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector(gray)

        for face in faces:
            landmarks = predictor(gray, face)
            # dlib face recognition expects RGB input
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            embedding = np.array(face_rec_model.compute_face_descriptor(frame_rgb, landmarks))

            # Draw rectangle around the face
            x, y, w, h = (face.left(), face.top(), face.width(), face.height())
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            cv2.putText(frame, "Press 's' to save", (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        cv2.imshow("Add Face", frame)

        key = cv2.waitKey(1)
        if key == ord('s') and len(faces) > 0:
            # Save the embedding and name to the database
            cursor.execute(
                "INSERT INTO faces (name, embedding, created_at, seen_count) VALUES (?, ?, datetime('now'), 0)",
                (name, embedding.tobytes()),
            )
            conn.commit()
            print(f"Face for {name} added successfully!")
            break
        elif key == ord('q'):
            break

    try:
        cap.release()
    except Exception:
        pass
    cv2.destroyAllWindows()

if __name__ == "__main__":
    name = input("Enter the name of the person: ")
    add_face(name)