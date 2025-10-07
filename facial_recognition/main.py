import cv2
import dlib
import numpy as np
import sqlite3

# Initialize face detector and shape predictor
detector = dlib.get_frontal_face_detector()
predictor = dlib.shape_predictor("models/shape_predictor_68_face_landmarks.dat")
face_rec_model = dlib.face_recognition_model_v1("models/dlib_face_recognition_resnet_model_v1.dat")

# Connect to SQLite database
db_path = "database/faces.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

def recognize_face():
    cap = cv2.VideoCapture(0)
    print("Press 'q' to quit.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector(gray)

        for face in faces:
            landmarks = predictor(gray, face)
            embedding = np.array(face_rec_model.compute_face_descriptor(frame, landmarks))

            # Compare with database
            cursor.execute("SELECT name, embedding FROM faces")
            rows = cursor.fetchall()
            recognized_name = "Unknown"
            min_distance = float("inf")

            for row in rows:
                name, db_embedding = row
                db_embedding = np.frombuffer(db_embedding, dtype=np.float64)
                distance = np.linalg.norm(embedding - db_embedding)
                if distance < 0.6 and distance < min_distance:  # Threshold = 0.6
                    recognized_name = name
                    min_distance = distance

            # Draw rectangle and name
            x, y, w, h = (face.left(), face.top(), face.width(), face.height())
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            cv2.putText(frame, recognized_name, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        cv2.imshow("Face Recognition", frame)

        key = cv2.waitKey(1)
        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    recognize_face()