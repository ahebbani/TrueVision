"""Top-level orchestrator for TrueVision.

Responsibilities:
- Parse flags/environment, configure subsystems
- Wire camera, recognizer, audio transcription, OLED, and DB
- Drive the main loop; subsystems encapsulate specific logic

Run:
    python main.py [--flags]
"""
from __future__ import annotations

import os
import argparse
import platform
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from data_access import open_db, prune_embeddings_if_needed, MAX_TEMPLATES_PER_PERSON
from data_access.db import DB_PATH as DB_PATH_DEFAULT
from facial_recognition.camera import open_camera
from facial_recognition.recognizer import Recognizer, RecognizerConfig

# Paths to facial recognition resources (models remain under subpackage directory)
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
FACE_MODULE_DIR = os.path.join(ROOT_DIR, 'facial_recognition')
MODELS_DIR = os.path.join(FACE_MODULE_DIR, 'models')
RECORDINGS_DIR = os.path.join(ROOT_DIR, 'data', 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)

OVERLAY_ONLY_DEFAULT = False  # Draw overlays on black background

# Database connection
conn = open_db()
cursor = conn.cursor()

# Optional OLED
try:
    from oled_output.oled_display import get_display
    _oled = get_display()
except Exception:
    _oled = None

def parse_args():
    p = argparse.ArgumentParser(description="TrueVision runtime")
    # Camera flags
    p.add_argument('--camera-backend', default=os.environ.get('CAMERA_BACKEND', 'auto'), choices=['auto','opencv','gstreamer','picamera2'])
    p.add_argument('--camera-index', type=int, default=int(os.environ.get('CAMERA_INDEX', '0')))
    p.add_argument('--camera-width', type=int, default=640)
    p.add_argument('--camera-height', type=int, default=480)
    p.add_argument('--camera-fps', type=int, default=30)
    # Face recognizer flags
    p.add_argument('--face-detector', default=os.environ.get('FACE_DETECTOR', 'auto'), choices=['auto','hog','cnn'])
    p.add_argument('--match-threshold', type=float, default=0.6)
    p.add_argument('--quality-min-var', type=float, default=120.0)
    p.add_argument('--diversity-min-dist', type=float, default=0.20)
    p.add_argument('--add-cooldown-sec', type=float, default=5.0)
    p.add_argument('--template-verbose', action='store_true', help='Verbose logs for template add/skip decisions')
    p.add_argument('--absence-grace-sec', type=float, default=2.0)
    # Transcription flags
    p.add_argument('--whisper-model', default=os.environ.get('WHISPER_MODEL', 'tiny'))
    p.add_argument('--caption-interval', type=float, default=0.7, help='Seconds between caption updates')
    p.add_argument('--caption-max-words', type=int, default=30)
    p.add_argument('--caption-max-lines', type=int, default=2)
    # Caption speech (TTS)
    p.add_argument(
        '--speak-captions',
        action='store_true',
        default=bool(int(os.environ.get('SPEAK_CAPTIONS', '0'))),
        help='Speak generated live captions (best-effort; requires TTS backend)'
    )
    p.add_argument(
        '--speech-rate',
        type=int,
        default=int(os.environ.get('SPEECH_RATE', '175')),
        help='Speech rate in words per minute (backend-dependent)'
    )
    p.add_argument(
        '--speech-volume',
        type=float,
        default=float(os.environ.get('SPEECH_VOLUME', '1.0')),
        help='Speech volume 0.0-1.0 (backend-dependent)'
    )
    # Audio source flags
    p.add_argument('--audio-source', default='auto', choices=['auto', 'sounddevice', 'esp32-serial'],
                   help='Audio input source: auto (prefer ESP32 UART if streaming), sounddevice (local mic), or esp32-serial (force ESP32 via UART)')
    p.add_argument('--serial-port', default='/dev/serial0', help='Serial port for ESP32 audio (default: /dev/serial0)')
    p.add_argument('--serial-baud', type=int, default=921600, help='Baud rate for ESP32 serial (default: 921600)')
    # UI/overlay flags
    p.add_argument('--overlay-only', action='store_true', default=OVERLAY_ONLY_DEFAULT)
    # Audio enable/disable flags
    p.add_argument('--audio', dest='audio', action='store_true', default=True, help='Enable audio recording/transcription (default)')
    p.add_argument('--no-audio', dest='audio', action='store_false', help='Disable audio recording/transcription for performance')
    # Summary flags
    p.add_argument('--summary-async', action='store_true', help='Compute meeting summaries in a background thread')
    p.add_argument('--summary-max-sentences', type=int, default=5)
    return p.parse_args()


def recognize_face():
    args = parse_args()
    cap = open_camera(args.camera_backend, args.camera_index, args.camera_width, args.camera_height, args.camera_fps)
    if cap is None:
        print("ERROR: Could not open any camera. On Raspberry Pi, ensure libcamera works (try: libcamera-hello).\n"
              "Install either python3-opencv with GStreamer support, or python3-picamera2.")
        return
    print("Press 'q' to quit.")
    if args.audio:
        print("Press 't' to toggle transcription on/off (default: ON).")
    else:
        print("Audio disabled (--no-audio). Transcription is OFF.")

    presence_state = {}
    last_detected_ts = {}
    ABSENCE_GRACE_SEC = args.absence_grace_sec

    # Recognizer
    recog = Recognizer(RecognizerConfig(
        models_dir=MODELS_DIR,
        detector_mode=args.face_detector,
        match_threshold=args.match_threshold,
        quality_min_var=args.quality_min_var,
        diversity_min_dist=args.diversity_min_dist,
        add_cooldown_sec=args.add_cooldown_sec,
        verbose=bool(getattr(args, 'template_verbose', False)),
    ))

    transcription_enabled = args.audio
    active_recorders = {}
    active_meetings = {}
    live_captions = {}
    Recorder = None
    create_recorder = None
    Transcriber = None
    def summarize_text(txt: str, max_sentences: int = 5) -> str:
        return ''
    if transcription_enabled:
        try:
            from audio_analysis.transcription import (
                Recorder as _Recorder,
                create_recorder as _create_recorder,
                Transcriber as _Transcriber,
                summarize_text as _summarize_text,
            )
            Recorder, create_recorder, Transcriber, summarize_text = _Recorder, _create_recorder, _Transcriber, _summarize_text
        except Exception as e:
            print(f"WARNING: Transcription modules unavailable ({e}). Transcription disabled.")
            transcription_enabled = False
    else:
        print("INFO: Skipping audio subsystem initialization (disabled by flag).")

    transcriber: Optional[object] = None
    if transcription_enabled and Transcriber is not None:
        try:
            transcriber = Transcriber(model_size=args.whisper_model)
        except Exception as e:
            print(f"WARNING: Transcriber initialization failed ({e}). Transcription disabled.")
            transcription_enabled = False

    # Optional caption speaker (TTS)
    speaker = None
    if getattr(args, 'speak_captions', False):
        try:
            from audio_analysis.caption_speaker import CaptionSpeaker, CaptionSpeakerConfig

            speaker = CaptionSpeaker(
                CaptionSpeakerConfig(
                    enabled=True,
                    rate_wpm=int(getattr(args, 'speech_rate', 175)),
                    volume=float(getattr(args, 'speech_volume', 1.0)),
                )
            )
            if not getattr(speaker, 'available', False):
                print("WARNING: --speak-captions enabled but no TTS backend found (install pyttsx3 or espeak).")
        except Exception as e:
            print(f"WARNING: Caption speaker init failed ({e}). Speech disabled.")
            speaker = None

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

        display_frame = np.zeros_like(frame) if args.overlay_only else frame.copy()

        recognized_ids_in_frame = set()
        faces_info = recog.detect_and_recognize(conn, frame)
        for info in faces_info:
            x, y, w, h = info.rect
            recognized_id = info.person_id
            recognized_name = info.name
            recognized_seen_count = info.seen_count
            recognized_last_seen_str = info.last_seen_at

            if recognized_id is not None:
                now_ts = time.time()
                prev_state = presence_state.get(recognized_id, 'absent')
                recognized_ids_in_frame.add(recognized_id)
                last_detected_ts[recognized_id] = now_ts

                if prev_state != 'present':
                    recog.update_seen(conn, recognized_id)
                    presence_state[recognized_id] = 'present'
                    if recognized_seen_count is not None:
                        recognized_seen_count += 1
                    recognized_last_seen_str = "now"
                    _oled_show_person(recognized_name, recognized_seen_count, recognized_last_seen_str, transcription_enabled)
                    if transcription_enabled and recognized_id not in active_recorders:
                        if create_recorder:
                            rec = create_recorder(
                                audio_source=args.audio_source,
                                serial_port=args.serial_port,
                                serial_baud=args.serial_baud
                            )
                        else:
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

                # Adaptive template add
                if recognized_id is not None and info.embedding is not None:
                    if recog.maybe_add_embedding(conn, recognized_id, info.embedding, info.quality):
                        prune_embeddings_if_needed(conn, recognized_id, MAX_TEMPLATES_PER_PERSON)

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
            from audio_analysis.live_caption import LiveCaptioner, CaptionConfig
            # Initialize once and cache on function attribute
            if not hasattr(recognize_face, "_captioner"):
                recognize_face._captioner = LiveCaptioner(transcriber, CaptionConfig(interval_sec=args.caption_interval, max_words=args.caption_max_words))
            captioner = recognize_face._captioner  # type: ignore[attr-defined]
            captioner.update(active_recorders, active_meetings, cursor)
            caption = captioner.get_caption_for_present(presence_state)
            # Draw captions at bottom, wrap to fit
            if caption:
                if speaker is not None:
                    try:
                        speaker.submit(caption)
                    except Exception:
                        pass
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
                    ((tw, _th), _) = cv2.getTextSize(test, font, font_scale, thickness)
                    if tw + margin*2 <= img_w:
                        current = test
                    else:
                        if current:
                            lines.append(current)
                        current = w
                if current:
                    lines.append(current)
                lines = lines[-int(args.caption_max_lines):]
                line_height = int(cv2.getTextSize("Ag", font, font_scale, thickness)[0][1] * 1.6)
                box_height = line_height * len(lines) + margin*2
                y0 = max(0, img_h - box_height)
                cv2.rectangle(display_frame, (0, y0), (img_w, img_h), (0, 0, 0), -1)
                y = y0 + margin + int(line_height * 0.8)
                for ln in lines:
                    cv2.putText(display_frame, ln, (margin, y), font, font_scale, (255, 255, 255), thickness)
                    y += line_height

        if _oled and len(faces_info) == 0 and all(v != 'present' for v in presence_state.values()):
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
                        if meeting_id is not None:
                            # Compute transcript synchronously (if possible) so text appears quickly
                            transcript_text = None
                            if audio_path_final and transcription_enabled and transcriber is not None:
                                try:
                                    transcript_text = transcriber.transcribe(audio_path_final)
                                except Exception as e:
                                    print(f"Transcription failed: {e}")
                                    transcript_text = None

                            # Fallback to existing incremental transcript
                            if transcript_text is None:
                                cursor.execute("SELECT COALESCE(transcript,'') FROM meetings WHERE id = ?", (meeting_id,))
                                (existing_transcript,) = cursor.fetchone() or ('',)
                                transcript_text = existing_transcript or ''

                            # Always set ended_at and transcript now
                            cursor.execute(
                                "UPDATE meetings SET ended_at = datetime('now'), transcript = ? WHERE id = ?",
                                (transcript_text, meeting_id),
                            )
                            conn.commit()

                            # Summary: async if requested, else synchronous
                            if getattr(args, 'summary_async', False):
                                import threading

                                def _bg_summarize(mid: int, db_path: str, text: str, max_sent: int):
                                    try:
                                        from data_access.db import open_db as _open_db
                                        from audio_analysis.transcription import summarize_text as _summ
                                        c2 = _open_db(db_path)
                                        cur2 = c2.cursor()
                                        summary = _summ(text, max_sentences=max_sent)
                                        cur2.execute(
                                            "UPDATE meetings SET summary = ? WHERE id = ?",
                                            (summary, mid),
                                        )
                                        c2.commit()
                                        c2.close()
                                    except Exception as _e:
                                        print(f"Background summary failed: {_e}")

                                t = threading.Thread(target=_bg_summarize, args=(meeting_id, DB_PATH_DEFAULT, transcript_text, int(getattr(args, 'summary_max_sentences', 5)))),
                                # unpack tuple accidental trailing comma avoidance
                                t = t[0]
                                t.daemon = True
                                t.start()
                            else:
                                summary_text = summarize_text(transcript_text, max_sentences=args.summary_max_sentences)
                                cursor.execute(
                                    "UPDATE meetings SET summary = ? WHERE id = ?",
                                    (summary_text, meeting_id),
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
        if speaker is not None:
            speaker.close()
    except Exception:
        pass
    try:
        if _oled:
            _oled.clear()
    except Exception:
        pass


if __name__ == "__main__":
    recognize_face()
