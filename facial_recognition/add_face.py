import cv2
import dlib
import numpy as np
import sqlite3
import os
import time

# Initialize face detector and shape predictor
detector = dlib.get_frontal_face_detector()
predictor = dlib.shape_predictor("models/shape_predictor_68_face_landmarks.dat")
face_rec_model = dlib.face_recognition_model_v1("models/dlib_face_recognition_resnet_model_v1.dat")

# Connect to SQLite database
db_path = "database/faces.db"
os.makedirs("database", exist_ok=True)
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create table and ensure columns if they don't exist
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS faces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        embedding BLOB NOT NULL
    )
    """
)
cursor.execute("PRAGMA table_info(faces)")
cols = {row[1] for row in cursor.fetchall()}
if "created_at" not in cols:
    cursor.execute("ALTER TABLE faces ADD COLUMN created_at TEXT DEFAULT (datetime('now'))")
if "last_seen_at" not in cols:
    cursor.execute("ALTER TABLE faces ADD COLUMN last_seen_at TEXT")
if "seen_count" not in cols:
    cursor.execute("ALTER TABLE faces ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 0")
conn.commit()

def add_face(name):
    cap = cv2.VideoCapture(0)
    print("Press 's' to capture the face and save it.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector(gray)

        for face in faces:
            landmarks = predictor(gray, face)
            embedding = np.array(face_rec_model.compute_face_descriptor(frame, landmarks))

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

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    name = input("Enter the name of the person: ")
    add_face(name)