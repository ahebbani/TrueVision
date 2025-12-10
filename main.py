"""Top-level application entry point for TrueVision.

This consolidates functionality from component packages:
- facial_recognition: core face detection & recognition logic + models
- audio_analysis: optional transcription & summarization
- oled_output: optional SSD1306 OLED status display
- data_access: centralized SQLite schema & pruning utilities

Run:
    python main.py

or:
    python -m main  (if treated as a module in some contexts)
"""
from __future__ import annotations

import os
import platform
import time
from datetime import datetime
from typing import Optional

import cv2
import dlib
import numpy as np

from data_access import open_db, prune_embeddings_if_needed, MAX_TEMPLATES_PER_PERSON

# Paths to facial recognition resources (models remain under subpackage directory)
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
FACE_MODULE_DIR = os.path.join(ROOT_DIR, 'facial_recognition')
MODELS_DIR = os.path.join(FACE_MODULE_DIR, 'models')
RECORDINGS_DIR = os.path.join(ROOT_DIR, 'data', 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# Initialize dlib components
detector = dlib.get_frontal_face_detector()
predictor = dlib.shape_predictor(os.path.join(MODELS_DIR, 'shape_predictor_68_face_landmarks.dat'))
face_rec_model = dlib.face_recognition_model_v1(os.path.join(MODELS_DIR, 'dlib_face_recognition_resnet_model_v1.dat'))

DETECTOR_MODE = os.environ.get('FACE_DETECTOR', 'auto')  # 'auto' | 'hog' | 'cnn'
cnn_model_path = os.path.join(MODELS_DIR, 'mmod_human_face_detector.dat')
cnn_detector = None
if DETECTOR_MODE in ('auto', 'cnn') and os.path.exists(cnn_model_path):
    try:
        cnn_detector = dlib.cnn_face_detection_model_v1(cnn_model_path)
    except Exception:
        cnn_detector = None

OVERLAY_ONLY = False  # Draw overlays on black background

# Database connection
conn = open_db()
cursor = conn.cursor()

# Optional OLED
try:
    from oled_output.oled_display import get_display
    _oled = get_display()
except Exception:
    _oled = None

CAMERA_BACKEND = os.environ.get('CAMERA_BACKEND', 'auto')
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))


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


def recognize_face():
    cap = open_camera()
    if cap is None:
        print("ERROR: Could not open any camera. On Raspberry Pi, ensure libcamera works (try: libcamera-hello).\n"
              "Install either python3-opencv with GStreamer support, or python3-picamera2.")
        return
    print("Press 'q' to quit.")
    print("Press 't' to toggle transcription on/off (default: ON).")

    presence_state = {}
    last_detected_ts = {}
    ABSENCE_GRACE_SEC = 2.0

    last_added_ts = {}
    ADD_COOLDOWN_SEC = 5.0
    DIVERSITY_MIN_DIST = 0.20
    QUALITY_MIN_VAR = 120.0

    transcription_enabled = True
    active_recorders = {}
    active_meetings = {}
    live_captions = {}
    last_live_update = {}
    Recorder = None
    Transcriber = None
    summarize_text = lambda txt: ''
    try:
        from audio_analysis.transcription import (
            Recorder as _Recorder,
            Transcriber as _Transcriber,
            summarize_text as _summarize_text,
        )
        Recorder, Transcriber, summarize_text = _Recorder, _Transcriber, _summarize_text
    except Exception as e:
        print(f"WARNING: Transcription modules unavailable ({e}). Transcription disabled.")
        transcription_enabled = False

    transcriber: Optional[object] = None
    if transcription_enabled and Transcriber is not None:
        try:
            transcriber = Transcriber(model_size=os.environ.get('WHISPER_MODEL', 'tiny'))
        except Exception as e:
            print(f"WARNING: Transcriber initialization failed ({e}). Transcription disabled.")
            transcription_enabled = False

    def _oled_show_person(name: str, seen_count: Optional[int], last_seen: Optional[str], rec: bool):
        if not _oled:
            return
        lines = [name or "Unknown"]
        meta = []
        if seen_count is not None:
            meta.append(f"seen {seen_count}")
        if last_seen:
            meta.append(f"last {last_seen}")
        if meta:
            lines.append(" • ".join(meta))
        if rec:
            lines.append("REC")
        _oled.update_text(lines)

    def _oled_idle():
        if not _oled:
            return
        _oled.update_text(["No one", datetime.now().strftime("%H:%M:%S")])

    try:
        if _oled:
            _oled.update_text(["Ready", datetime.now().strftime("%H:%M:%S")])
    except Exception:
        pass

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        display_frame = np.zeros_like(frame) if OVERLAY_ONLY else frame.copy()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if cnn_detector is not None and DETECTOR_MODE in ('auto', 'cnn'):
            dets = cnn_detector(gray, 1)
            faces = [d.rect for d in dets]
        else:
            faces = detector(gray)

        recognized_ids_in_frame = set()

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
            meta_by_person[person_id] = (name, db_seen_count, db_last_seen_at)

        for face in faces:
            landmarks = predictor(gray, face)
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            emb_live = np.array(face_rec_model.compute_face_descriptor(frame_rgb, landmarks))

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

            if recognized_id is not None:
                now_ts = time.time()
                prev_state = presence_state.get(recognized_id, 'absent')
                recognized_ids_in_frame.add(recognized_id)
                last_detected_ts[recognized_id] = now_ts

                if prev_state != 'present':
                    cursor.execute(
                        "UPDATE faces SET last_seen_at = datetime('now'), seen_count = seen_count + 1 WHERE id = ?",
                        (recognized_id,),
                    )
                    conn.commit()
                    presence_state[recognized_id] = 'present'
                    if recognized_seen_count is not None:
                        recognized_seen_count += 1
                    recognized_last_seen_str = "now"
                    _oled_show_person(recognized_name, recognized_seen_count, recognized_last_seen_str, transcription_enabled)
                    if transcription_enabled and recognized_id not in active_recorders:
                        rec = Recorder()
                        audio_path_pending = rec.start(RECORDINGS_DIR, f"person{recognized_id}")
                        cursor.execute(
                            "INSERT INTO meetings (person_id, started_at, audio_path) VALUES (?, datetime('now'), ?)",
                            (recognized_id, audio_path_pending),
                        )
                        meeting_id = cursor.lastrowid
                        conn.commit()
                        active_recorders[recognized_id] = rec
                        active_meetings[recognized_id] = meeting_id
                else:
                    presence_state[recognized_id] = 'present'

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
                            embeddings_by_person.setdefault(recognized_id, []).append(emb_live)
                            prune_embeddings_if_needed(conn, recognized_id, MAX_TEMPLATES_PER_PERSON)
                        except Exception:
                            pass

            x, y, w, h = (face.left(), face.top(), face.width(), face.height())
            cv2.rectangle(display_frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            label = recognized_name
            if recognized_id is not None and recognized_seen_count is not None:
                label = f"{recognized_name} (seen {recognized_seen_count})"
            cv2.putText(display_frame, label, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if recognized_id is not None:
                last_label = f"Last: {recognized_last_seen_str if recognized_last_seen_str else '—'}"
                cv2.putText(display_frame, last_label, (x, y+15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                if transcription_enabled and recognized_id in active_recorders:
                    cv2.putText(display_frame, "REC", (x, y+30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        # Live transcription overlay (closed captioning)
        if transcription_enabled and transcriber is not None and active_recorders:
            now = time.time()
            for pid, rec in list(active_recorders.items()):
                audio_path = rec.audio_path
                if not audio_path:
                    continue
                last_ts = last_live_update.get(pid, 0.0)
                if (now - last_ts) >= 0.7:  # throttle updates (more frequent)
                    try:
                        text_live = transcriber.transcribe(audio_path)
                        last_live_update[pid] = now
                        # Keep only a short tail for overlay
                        tail = text_live.strip().split()
                        tail_txt = " ".join(tail[-30:])  # ~ last few words
                        live_captions[pid] = tail_txt
                        # Optionally persist incremental transcript to DB
                        mid = active_meetings.get(pid)
                        if mid is not None and text_live:
                            cursor.execute(
                                "UPDATE meetings SET transcript = ? WHERE id = ?",
                                (text_live, mid),
                            )
                            conn.commit()
                    except Exception:
                        pass
            # Draw the most recent caption for any present person at bottom
            if live_captions:
                caption = None
                # Prefer caption of someone currently present
                for pid, state in presence_state.items():
                    if state == 'present' and pid in live_captions:
                        caption = live_captions.get(pid)
                        break
                if caption is None:
                    # fallback to any caption
                    caption = next(iter(live_captions.values()))
                if caption:
                    # Wrap caption text to fit window width
                    img_h, img_w = display_frame.shape[0], display_frame.shape[1]
                    margin = 10
                    font = cv2.FONT_HERSHEY_SIMPLEX
                    font_scale = 0.6
                    thickness = 2

                    words = caption.split()
                    lines = []
                    current = ""
                    for w in words:
                        test = (current + (" " if current else "") + w)
                        ((tw, th), _) = cv2.getTextSize(test, font, font_scale, thickness)
                        if tw + margin*2 <= img_w:
                            current = test
                        else:
                            if current:
                                lines.append(current)
                            current = w
                    if current:
                        lines.append(current)

                    # Limit lines to 2 for compactness
                    lines = lines[-2:]
                    line_height = int(cv2.getTextSize("Ag", font, font_scale, thickness)[0][1] * 1.6)
                    box_height = line_height * len(lines) + margin*2
                    y0 = max(0, img_h - box_height)
                    cv2.rectangle(display_frame, (0, y0), (img_w, img_h), (0, 0, 0), -1)
                    y = y0 + margin + int(line_height * 0.8)
                    for ln in lines:
                        cv2.putText(display_frame, ln, (margin, y), font, font_scale, (255, 255, 255), thickness)
                        y += line_height

        if _oled and len(faces) == 0 and all(v != 'present' for v in presence_state.values()):
            _oled_idle()

        now_ts = time.time()
        for pid, state in list(presence_state.items()):
            if state == 'present' and pid not in recognized_ids_in_frame:
                last_ts = last_detected_ts.get(pid)
                if last_ts is not None and (now_ts - last_ts) > ABSENCE_GRACE_SEC:
                    presence_state[pid] = 'absent'
                    if pid in active_recorders:
                        rec = active_recorders.pop(pid)
                        meeting_id = active_meetings.pop(pid, None)
                        audio_path_final = rec.stop()
                        if meeting_id is not None and audio_path_final and transcription_enabled and transcriber is not None:
                            try:
                                transcript_text = transcriber.transcribe(audio_path_final)
                            except Exception as e:
                                print(f"Transcription failed: {e}")
                                transcript_text = ''
                            # Summary can be backfilled later; compute now if lightweight
                            summary_text = summarize_text(transcript_text)
                            cursor.execute(
                                "UPDATE meetings SET ended_at = datetime('now'), transcript = ?, summary = ? WHERE id = ?",
                                (transcript_text, summary_text, meeting_id),
                            )
                            conn.commit()
                    if _oled and all(v == 'absent' for v in presence_state.values()):
                        _oled_idle()

        cv2.imshow("Face Recognition", display_frame)
        key = cv2.waitKey(1)
        if key == ord('q'):
            break
        if key == ord('t'):
            transcription_enabled = not transcription_enabled
            state_txt = 'ENABLED' if transcription_enabled else 'DISABLED'
            print(f"Transcription {state_txt}")
            if not transcription_enabled:
                for pid, rec in list(active_recorders.items()):
                    rec.stop()
                active_recorders.clear()
                active_meetings.clear()

    try:
        cap.release()
    except Exception:
        pass
    cv2.destroyAllWindows()
    try:
        if _oled:
            _oled.clear()
    except Exception:
        pass


if __name__ == "__main__":
    recognize_face()
