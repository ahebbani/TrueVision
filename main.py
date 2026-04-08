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
import queue
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from data_access import (
    open_db,
    prune_embeddings_if_needed,
    MAX_TEMPLATES_PER_PERSON,
    get_latest_finished_meeting,
)
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
    from oled_output.oled_display import get_display, _DummyDisplay
    _oled = get_display()
    _oled_missing = isinstance(_oled, _DummyDisplay)
except Exception:
    _oled = None
    _oled_missing = True

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
    p.add_argument('--serial-baud', type=int, default=460800, help='Baud rate for ESP32 serial (default: 460800; use this on Pi 5 — 921600 is unreliable on the RP1 UART)')
    p.add_argument('--no-mode-gate', action='store_true', default=False,
                   help='Disable ESP32 mode-based face/audio gating — run both simultaneously. '
                        'Use this when the hardware mode switch is not connected.')
    # UI/overlay flags
    p.add_argument('--overlay-only', action='store_true', default=OVERLAY_ONLY_DEFAULT)
    # Audio enable/disable flags
    p.add_argument('--audio', dest='audio', action='store_true', default=True, help='Enable audio recording/transcription (default)')
    p.add_argument('--no-audio', dest='audio', action='store_false', help='Disable audio recording/transcription for performance')
    # Summary flags
    p.add_argument('--summary-async', action='store_true', help='Compute meeting summaries in a background thread')
    p.add_argument('--summary-max-sentences', type=int, default=1)
    p.add_argument('--summary-max-chars', type=int, default=140, help='Clamp 1-sentence summaries to this many chars (default: %(default)s)')
    p.add_argument('--prev-summary-max-chars', type=int, default=140, help='Clamp displayed previous-conversation summary (default: %(default)s)')
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

    # ── ESP32 mode switch and marker integration ──────────────────────────────
    # current_mode is a one-element list so the closures below can mutate it.
    from audio_analysis.esp32_serial_audio import MODE_AUDIO, MODE_FACE
    current_mode = [MODE_AUDIO]
    # Thread-safe queue for marker events (PKT_MARKER from ESP32 button).
    # The main loop drains the queue and appends [MARKER HH:MM:SS] to the
    # active meeting transcript.
    _marker_queue: queue.Queue = queue.Queue()

    def _on_mode_change(mode_byte: int) -> None:
        current_mode[0] = mode_byte
        label = "FACE" if mode_byte == MODE_FACE else "AUDIO"
        print(f"ESP32: Mode changed to {label}")

    def _on_marker() -> None:
        _marker_queue.put(datetime.now().strftime("%H:%M:%S"))

    def _on_diag_request() -> None:
        # Diagnostic handler: if OLED is absent the send_pi_status call inside
        # _receiver_loop already pushes current status.  Log here for visibility.
        print("ESP32: DIAG_REQUEST received; status pushed to ESP32.")

    # Wire up callbacks on the shared receiver if using esp32-serial audio.
    # For 'auto', do a quick probe so we set callbacks before the main loop.
    _esp32_receiver = None
    if args.audio_source in ('esp32-serial', 'auto'):
        try:
            from audio_analysis.transcription import get_shared_receiver
            from audio_analysis.esp32_serial_audio import probe_esp32_uart_stream
            should_init = (
                args.audio_source == 'esp32-serial'
                or (probe_esp32_uart_stream is not None and
                    probe_esp32_uart_stream(port=args.serial_port,
                                            baud_rate=args.serial_baud,
                                            timeout_sec=1.0))
            )
            if should_init:
                _esp32_receiver = get_shared_receiver(
                    serial_port=args.serial_port,
                    serial_baud=args.serial_baud,
                    oled_missing=_oled_missing,
                    on_mode_change=_on_mode_change,
                    on_marker=_on_marker,
                    on_diag_request=_on_diag_request,
                )
        except Exception as _recv_err:
            print(f"WARNING: Could not initialise ESP32 receiver for callbacks: {_recv_err}")

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

    def _oled_show_person(name: str, seen_count: Optional[int], last_seen: Optional[str], rec: bool, prev_summary: Optional[str] = None):
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
        if prev_summary:
            lines.append(prev_summary)
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

        # ── Drain ESP32 marker events ─────────────────────────────────────────
        # The button short-press sends PKT_MARKER; we insert a timestamp tag
        # into the transcript of every active meeting.
        while not _marker_queue.empty():
            try:
                marker_ts = _marker_queue.get_nowait()
                marker_tag = f" [MARKER {marker_ts}]"
                for pid, mid in list(active_meetings.items()):
                    try:
                        cursor.execute(
                            "UPDATE meetings SET transcript = COALESCE(transcript,'') || ? WHERE id = ?",
                            (marker_tag, mid),
                        )
                        conn.commit()
                        print(f"Marker inserted into meeting {mid} at {marker_ts}")
                    except Exception as _me:
                        print(f"WARNING: Could not insert marker into meeting {mid}: {_me}")
            except queue.Empty:
                break

        recognized_ids_in_frame = set()
        # Skip face detection only when the ESP32 is connected AND in AUDIO mode,
        # unless --no-mode-gate is set (for testing without the hardware switch).
        _mode_gate_active = (_esp32_receiver is not None) and not getattr(args, 'no_mode_gate', False)
        _skip_face = _mode_gate_active and (current_mode[0] == MODE_AUDIO)
        faces_info = [] if _skip_face else recog.detect_and_recognize(conn, frame)
        if not hasattr(recognize_face, "_prev_summaries"):
            recognize_face._prev_summaries = {}
        prev_summaries = recognize_face._prev_summaries  # type: ignore[attr-defined]
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
                    # Fetch previous-conversation summary for display.
                    prev_summary = ""
                    try:
                        row = get_latest_finished_meeting(conn, recognized_id)
                        if row:
                            prev_mid, _ended_at, prev_transcript, prev_summary_db = row
                            try:
                                from audio_analysis.transcription import summarize_one_sentence

                                if (prev_summary_db or "").strip():
                                    prev_summary = summarize_one_sentence(
                                        str(prev_summary_db),
                                        max_chars=int(getattr(args, "prev_summary_max_chars", 140)),
                                    )
                                elif prev_transcript:
                                    prev_summary = summarize_one_sentence(
                                        prev_transcript,
                                        max_chars=int(getattr(args, "prev_summary_max_chars", 140)),
                                    )
                                    if prev_summary:
                                        cursor.execute(
                                            "UPDATE meetings SET summary = ? WHERE id = ?",
                                            (prev_summary, int(prev_mid)),
                                        )
                                        conn.commit()
                            except Exception:
                                prev_summary = (prev_summary_db or "").strip()
                            if row and not (prev_summary or "").strip():
                                prev_summary = "no summary available"
                    except Exception:
                        prev_summary = ""
                    prev_summaries[int(recognized_id)] = prev_summary

                    recog.update_seen(conn, recognized_id)
                    presence_state[recognized_id] = 'present'
                    if recognized_seen_count is not None:
                        recognized_seen_count += 1
                    recognized_last_seen_str = "now"
                    _oled_show_person(
                        recognized_name,
                        recognized_seen_count,
                        recognized_last_seen_str,
                        transcription_enabled,
                        prev_summary=prev_summary,
                    )
                    if transcription_enabled and recognized_id not in active_recorders and current_mode[0] != MODE_FACE:
                        if create_recorder:
                            rec = create_recorder(
                                audio_source=args.audio_source,
                                serial_port=args.serial_port,
                                serial_baud=args.serial_baud
                            )
                        else:
                            rec = Recorder()
                        try:
                            audio_path_pending = rec.start(RECORDINGS_DIR, f"person{recognized_id}")
                        except Exception as _rec_err:
                            print(f"WARNING: Could not start audio recorder ({_rec_err}). "
                                  f"Transcription disabled. Use --no-audio to suppress this warning.")
                            transcription_enabled = False
                            rec = None
                        if rec is not None:
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
                prev_summary = prev_summaries.get(int(recognized_id), "")
                if prev_summary:
                    # Keep overlay compact.
                    prev_line = prev_summary
                    max_chars = 48
                    if len(prev_line) > max_chars:
                        prev_line = prev_line[: max_chars - 1].rstrip() + "…"
                    cv2.putText(display_frame, f"Prev: {prev_line}", (x, y+30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
                if transcription_enabled and recognized_id in active_recorders:
                    cv2.putText(display_frame, "REC", (x, y+45), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

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

                                def _bg_summarize(mid: int, db_path: str, text: str, max_sent: int, max_chars: int):
                                    try:
                                        from data_access.db import open_db as _open_db
                                        from audio_analysis.transcription import summarize_one_sentence as _summ1
                                        from audio_analysis.transcription import summarize_text as _summ
                                        # Optional remote summarizer (off-device)
                                        try:
                                            from summarization.remote_client import remote_summarize_one_sentence as _remote
                                        except Exception:
                                            _remote = None
                                        c2 = _open_db(db_path)
                                        cur2 = c2.cursor()

                                        summary = ""
                                        # Prefer remote summarizer if configured.
                                        if _remote is not None:
                                            try:
                                                # Try to pass previous summary context for coherence.
                                                cur2.execute(
                                                    "SELECT person_id FROM meetings WHERE id = ?",
                                                    (int(mid),),
                                                )
                                                rpid = cur2.fetchone()
                                                person_id = int(rpid[0]) if rpid and rpid[0] is not None else None
                                                prev_summary = ""
                                                if person_id is not None:
                                                    cur2.execute(
                                                        """
                                                        SELECT COALESCE(summary,'') FROM meetings
                                                        WHERE person_id = ? AND ended_at IS NOT NULL AND id < ? AND COALESCE(summary,'') != ''
                                                        ORDER BY id DESC LIMIT 1
                                                        """,
                                                        (int(person_id), int(mid)),
                                                    )
                                                    rprev = cur2.fetchone()
                                                    prev_summary = (rprev[0] if rprev else "") or ""

                                                cur2.execute("SELECT COALESCE(name,'') FROM faces WHERE id = ?", (int(person_id),))
                                                rname = cur2.fetchone() if person_id is not None else None
                                                person_name = (rname[0] if rname else "") or None

                                                summary = _remote(
                                                    transcript=text,
                                                    previous_summary=prev_summary,
                                                    person_name=person_name,
                                                    max_chars=int(max_chars),
                                                )
                                            except Exception:
                                                summary = ""

                                        if not summary:
                                            if int(max_sent) <= 1:
                                                summary = _summ1(text, max_chars=int(max_chars))
                                            else:
                                                summary = _summ(text, max_sentences=int(max_sent))
                                        cur2.execute(
                                            "UPDATE meetings SET summary = ? WHERE id = ?",
                                            (summary, mid),
                                        )
                                        c2.commit()
                                        c2.close()
                                    except Exception as _e:
                                        print(f"Background summary failed: {_e}")

                                t = threading.Thread(
                                    target=_bg_summarize,
                                    args=(
                                        meeting_id,
                                        DB_PATH_DEFAULT,
                                        transcript_text,
                                        int(getattr(args, 'summary_max_sentences', 1)),
                                        int(getattr(args, 'summary_max_chars', 140)),
                                    ),
                                ),
                                # unpack tuple accidental trailing comma avoidance
                                t = t[0]
                                t.daemon = True
                                t.start()
                            else:
                                try:
                                    from audio_analysis.transcription import summarize_one_sentence
                                    try:
                                        from summarization.remote_client import remote_summarize_one_sentence
                                    except Exception:
                                        remote_summarize_one_sentence = None  # type: ignore

                                    if int(getattr(args, 'summary_max_sentences', 1)) <= 1:
                                        summary_text = ""
                                        if remote_summarize_one_sentence is not None:
                                            try:
                                                # Provide previous summary and person name if available.
                                                cursor.execute(
                                                    "SELECT COALESCE(name,'') FROM faces WHERE id = ?",
                                                    (int(pid),),
                                                )
                                                rname = cursor.fetchone()
                                                person_name = (rname[0] if rname else "") or None

                                                cursor.execute(
                                                    """
                                                    SELECT COALESCE(summary,'') FROM meetings
                                                    WHERE person_id = ? AND ended_at IS NOT NULL AND id < ? AND COALESCE(summary,'') != ''
                                                    ORDER BY id DESC LIMIT 1
                                                    """,
                                                    (int(pid), int(meeting_id)),
                                                )
                                                rprev = cursor.fetchone()
                                                prev_summary = (rprev[0] if rprev else "") or ""

                                                summary_text = remote_summarize_one_sentence(
                                                    transcript=transcript_text,
                                                    previous_summary=prev_summary,
                                                    person_name=person_name,
                                                    max_chars=int(getattr(args, 'summary_max_chars', 140)),
                                                )
                                            except Exception as e:
                                                print(f"Remote summarizer failed; falling back: {e}")
                                                summary_text = ""
                                        if not summary_text:
                                            summary_text = summarize_one_sentence(
                                                transcript_text,
                                                max_chars=int(getattr(args, 'summary_max_chars', 140)),
                                            )
                                    else:
                                        summary_text = summarize_text(transcript_text, max_sentences=args.summary_max_sentences)
                                except Exception:
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
