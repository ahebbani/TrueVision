import cv2
import dlib
import numpy as np
import sqlite3
import time
import os

# Initialize face detector and shape predictor
detector = dlib.get_frontal_face_detector()

# Dynamically construct the paths relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, 'models')
DB_DIR = os.path.join(BASE_DIR, 'database')
os.makedirs(DB_DIR, exist_ok=True)

predictor_path = os.path.join(MODELS_DIR, 'shape_predictor_68_face_landmarks.dat')
predictor = dlib.shape_predictor(predictor_path)

face_rec_model_path = os.path.join(MODELS_DIR, 'dlib_face_recognition_resnet_model_v1.dat')
face_rec_model = dlib.face_recognition_model_v1(face_rec_model_path)

# Connect to SQLite database (relative to this script)
db_path = os.path.join(DB_DIR, 'faces.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Ensure schema has last seen fields
def ensure_schema(connection):
    cur = connection.cursor()
    # Table may already exist (created by add_face.py), but ensure columns too
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            embedding BLOB NOT NULL
        )
        """
    )
    cur.execute("PRAGMA table_info(faces)")
    cols = {row[1] for row in cur.fetchall()}
    if "created_at" not in cols:
        cur.execute("ALTER TABLE faces ADD COLUMN created_at TEXT DEFAULT (datetime('now'))")
    if "last_seen_at" not in cols:
        cur.execute("ALTER TABLE faces ADD COLUMN last_seen_at TEXT")
    if "seen_count" not in cols:
        cur.execute("ALTER TABLE faces ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 0")
    connection.commit()

ensure_schema(conn)

def recognize_face():
    cap = cv2.VideoCapture(0)
    print("Press 'q' to quit.")

    # Throttle DB writes so we don't update every frame
    last_update = {}  # person_id -> last update ts
    COOLDOWN_SEC = 5.0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector(gray)

        for face in faces:
            landmarks = predictor(gray, face)
            embedding = np.array(face_rec_model.compute_face_descriptor(frame, landmarks))

            # Compare with database (also read seen_count and last_seen_at for UI)
            cursor.execute("SELECT id, name, embedding, seen_count, last_seen_at FROM faces")
            rows = cursor.fetchall()
            recognized_name = "Unknown"
            recognized_id = None
            recognized_seen_count = None
            recognized_last_seen_at = None
            min_distance = float("inf")

            for row in rows:
                person_id, name, db_embedding, db_seen_count, db_last_seen_at = row
                db_embedding = np.frombuffer(db_embedding, dtype=np.float64)
                distance = np.linalg.norm(embedding - db_embedding)
                if distance < 0.6 and distance < min_distance:  # Threshold = 0.6
                    recognized_name = name
                    recognized_id = person_id
                    recognized_seen_count = db_seen_count
                    recognized_last_seen_at = db_last_seen_at
                    min_distance = distance

            # Update last seen fields if recognized and cooldown passed
            if recognized_id is not None:
                now = time.time()
                last = last_update.get(recognized_id, 0.0)
                if now - last > COOLDOWN_SEC:
                    cursor.execute(
                        "UPDATE faces SET last_seen_at = datetime('now'), seen_count = seen_count + 1 WHERE id = ?",
                        (recognized_id,),
                    )
                    conn.commit()
                    last_update[recognized_id] = now
                    # Reflect updated values in UI variables
                    if recognized_seen_count is not None:
                        recognized_seen_count += 1

            # Draw rectangle and name
            x, y, w, h = (face.left(), face.top(), face.width(), face.height())
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            label = recognized_name
            if recognized_id is not None and recognized_seen_count is not None:
                label = f"{recognized_name} (seen {recognized_seen_count})"
            cv2.putText(frame, label, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        cv2.imshow("Face Recognition", frame)

        key = cv2.waitKey(1)
        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    recognize_face()