import cv2
import dlib
import numpy as np
import os
import sys

# Initialize face detector and shape predictor
detector = dlib.get_frontal_face_detector()

# Resolve paths relative to this file so it works from any CWD
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, 'models')

predictor_path = os.path.join(MODELS_DIR, 'shape_predictor_68_face_landmarks.dat')
predictor = dlib.shape_predictor(predictor_path)

face_rec_model_path = os.path.join(MODELS_DIR, 'dlib_face_recognition_resnet_model_v1.dat')
face_rec_model = dlib.face_recognition_model_v1(face_rec_model_path)

# Ensure repository root on sys.path when running as a script
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from data_access import insert_face_with_template, open_db
from facial_recognition.camera import open_camera

# Reuse centralized DB (schema ensured automatically)
conn = open_db()


def add_face(name):
    cap = open_camera()
    if cap is None:
        print("ERROR: Could not open camera. Ensure Picamera2 is installed and the RPi camera is connected.")
        return
    print("Press 's' to capture the face and save it.")
    selected_embedding = None
    selected_quality = None

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
            embedding = embedding.astype(np.float64)

            # Draw rectangle around the face
            x, y, w, h = (face.left(), face.top(), face.width(), face.height())
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            cv2.putText(frame, "Press 's' to save", (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
            face_gray = gray[y0:y1, x0:x1]
            quality = float(cv2.Laplacian(face_gray, cv2.CV_64F).var()) if face_gray.size > 0 else None
            selected_embedding = embedding
            selected_quality = quality

        cv2.imshow("Add Face", frame)

        key = cv2.waitKey(1)
        if key == ord('s') and selected_embedding is not None:
            face_id = insert_face_with_template(
                conn,
                name,
                selected_embedding.tobytes(),
                quality=selected_quality,
            )
            print(f"Face for {name} added successfully as id {face_id}!")
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