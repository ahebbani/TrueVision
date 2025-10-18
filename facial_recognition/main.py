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

# Optional CNN detector for better angle/occlusion robustness (uses GPU if dlib built with CUDA)
DETECTOR_MODE = os.environ.get('FACE_DETECTOR', 'auto')  # 'auto' | 'hog' | 'cnn'
cnn_model_path = os.path.join(MODELS_DIR, 'mmod_human_face_detector.dat')
cnn_detector = None
if DETECTOR_MODE in ('auto', 'cnn') and os.path.exists(cnn_model_path):
    try:
        cnn_detector = dlib.cnn_face_detection_model_v1(cnn_model_path)
    except Exception:
        cnn_detector = None

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
        # SQLite doesn't allow function defaults in ALTER; add column then backfill
        cur.execute("ALTER TABLE faces ADD COLUMN created_at TEXT")
        cur.execute("UPDATE faces SET created_at = datetime('now') WHERE created_at IS NULL")
    if "last_seen_at" not in cols:
        cur.execute("ALTER TABLE faces ADD COLUMN last_seen_at TEXT")
    if "seen_count" not in cols:
        cur.execute("ALTER TABLE faces ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 0")
    connection.commit()

ensure_schema(conn)

# Ensure secondary table for multiple embeddings per person and seed from existing data
def ensure_embeddings_schema(connection):
    cur = connection.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS face_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_id INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            quality REAL,
            FOREIGN KEY(face_id) REFERENCES faces(id)
        )
        """
    )
    # Seed from faces.embedding for any face that doesn't yet have entries
    cur.execute(
        """
        INSERT INTO face_embeddings (face_id, embedding, created_at)
        SELECT id, embedding, datetime('now') FROM faces
        WHERE embedding IS NOT NULL AND id NOT IN (SELECT face_id FROM face_embeddings)
        """
    )
    connection.commit()

ensure_embeddings_schema(conn)

MAX_TEMPLATES_PER_PERSON = 30

def prune_embeddings_if_needed(connection, face_id: int, max_count: int = MAX_TEMPLATES_PER_PERSON):
    cur = connection.cursor()
    cur.execute(
        "SELECT id, quality, created_at FROM face_embeddings WHERE face_id = ?",
        (face_id,),
    )
    rows = cur.fetchall()
    if len(rows) <= max_count:
        return
    # Sort by quality asc (None first), then by created_at asc (oldest first)
    def sort_key(r):
        rid, q, ts = r
        qv = -1.0 if q is None else float(q)
        return (qv, ts or '')
    rows_sorted = sorted(rows, key=sort_key)
    to_remove = rows_sorted[: max(0, len(rows_sorted) - max_count)]
    ids = [r[0] for r in to_remove]
    if ids:
        cur.executemany("DELETE FROM face_embeddings WHERE id = ?", [(i,) for i in ids])
        connection.commit()

def recognize_face():
    cap = cv2.VideoCapture(0)
    print("Press 'q' to quit.")

    # Session-based presence tracking: update only on absent -> present transitions
    presence_state = {}  # person_id -> 'present' | 'absent'
    last_detected_ts = {}  # person_id -> last timestamp this frame saw the person
    ABSENCE_GRACE_SEC = 2.0  # require person to be missing for this long before marking absent

    # Incremental sampling controls
    last_added_ts = {}  # person_id -> last time we added a template
    ADD_COOLDOWN_SEC = 5.0
    DIVERSITY_MIN_DIST = 0.20  # require new sample to differ from all existing by at least this
    QUALITY_MIN_VAR = 120.0    # blur threshold via variance of Laplacian

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Detect faces using selected detector
        if cnn_detector is not None and DETECTOR_MODE in ('auto', 'cnn'):
            dets = cnn_detector(gray, 1)
            faces = [d.rect for d in dets]
        else:
            faces = detector(gray)

        recognized_ids_in_frame = set()

        # Load all embeddings once per frame and prepare per-person collections
        cursor.execute(
            """
            SELECT fe.face_id, f.name, fe.embedding, f.seen_count, f.last_seen_at
            FROM face_embeddings fe
            JOIN faces f ON f.id = fe.face_id
            """
        )
        rows = cursor.fetchall()
        embeddings_by_person = {}
        meta_by_person = {}
        for person_id, name, db_embedding, db_seen_count, db_last_seen_at in rows:
            emb = np.frombuffer(db_embedding, dtype=np.float64)
            embeddings_by_person.setdefault(person_id, []).append(emb)
            # store last seen/meta: last one wins but values are same for a person in join
            meta_by_person[person_id] = (name, db_seen_count, db_last_seen_at)

        for face in faces:
            landmarks = predictor(gray, face)
            emb_live = np.array(face_rec_model.compute_face_descriptor(frame, landmarks))

            # Match to nearest template across all persons
            recognized_name = "Unknown"
            recognized_id = None
            recognized_seen_count = None
            recognized_last_seen_str = None
            min_distance = float("inf")

            for person_id, person_embs in embeddings_by_person.items():
                for db_emb in person_embs:
                    distance = np.linalg.norm(emb_live - db_emb)
                    if distance < 0.6 and distance < min_distance:
                        name, db_seen_count, db_last_seen_at = meta_by_person.get(person_id, ("Unknown", None, None))
                        recognized_name = name
                        recognized_id = person_id
                        recognized_seen_count = db_seen_count
                        recognized_last_seen_str = db_last_seen_at
                        min_distance = distance

            # Presence tracking and DB updates only on absent -> present transition
            if recognized_id is not None:
                now_ts = time.time()
                prev_state = presence_state.get(recognized_id, 'absent')
                recognized_ids_in_frame.add(recognized_id)
                last_detected_ts[recognized_id] = now_ts

                if prev_state != 'present':
                    # Transition: absent -> present (new entry)
                    cursor.execute(
                        "UPDATE faces SET last_seen_at = datetime('now'), seen_count = seen_count + 1 WHERE id = ?",
                        (recognized_id,),
                    )
                    conn.commit()
                    presence_state[recognized_id] = 'present'
                    if recognized_seen_count is not None:
                        recognized_seen_count += 1
                    recognized_last_seen_str = "now"
                else:
                    presence_state[recognized_id] = 'present'

                # Consider adding this as a new template for diversity and quality
                x, y, w, h = (face.left(), face.top(), face.width(), face.height())
                x0, y0 = max(0, x), max(0, y)
                x1, y1 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
                face_gray = gray[y0:y1, x0:x1]
                quality = None
                if face_gray.size > 0:
                    quality = float(cv2.Laplacian(face_gray, cv2.CV_64F).var())

                now_add = time.time()
                can_add = (
                    quality is not None and quality >= QUALITY_MIN_VAR and
                    (now_add - last_added_ts.get(recognized_id, 0.0)) >= ADD_COOLDOWN_SEC
                )

                if can_add:
                    person_embs = embeddings_by_person.get(recognized_id, [])
                    is_diverse = True
                    if person_embs:
                        dists = [np.linalg.norm(emb_live - e) for e in person_embs]
                        min_person_dist = min(dists)
                        is_diverse = min_person_dist >= DIVERSITY_MIN_DIST

                    if is_diverse:
                        try:
                            cursor.execute(
                                "INSERT INTO face_embeddings (face_id, embedding, created_at, quality) VALUES (?, ?, datetime('now'), ?)",
                                (recognized_id, emb_live.astype(np.float64).tobytes(), quality),
                            )
                            conn.commit()
                            last_added_ts[recognized_id] = now_add
                            # Update in-memory collection so immediate next comparisons include it
                            embeddings_by_person.setdefault(recognized_id, []).append(emb_live)
                            # Enforce cap per person to keep DB small and curated
                            prune_embeddings_if_needed(conn, recognized_id, MAX_TEMPLATES_PER_PERSON)
                        except Exception:
                            pass

            # Draw rectangle and labels
            x, y, w, h = (face.left(), face.top(), face.width(), face.height())
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            label = recognized_name
            if recognized_id is not None and recognized_seen_count is not None:
                label = f"{recognized_name} (seen {recognized_seen_count})"
            cv2.putText(frame, label, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if recognized_id is not None:
                last_label = f"Last: {recognized_last_seen_str if recognized_last_seen_str else '—'}"
                cv2.putText(frame, last_label, (x, y+15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Mark present -> absent when not seen for grace period
        now_ts = time.time()
        for pid, state in list(presence_state.items()):
            if state == 'present' and pid not in recognized_ids_in_frame:
                last_ts = last_detected_ts.get(pid)
                if last_ts is not None and (now_ts - last_ts) > ABSENCE_GRACE_SEC:
                    presence_state[pid] = 'absent'

        cv2.imshow("Face Recognition", frame)

        key = cv2.waitKey(1)
        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    recognize_face()