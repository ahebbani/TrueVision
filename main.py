"""Top-level orchestrator for TrueVision.

Responsibilities:
- Parse flags/environment, configure subsystems
- Wire camera, recognizer, audio transcription, and DB
- Drive the main loop; subsystems encapsulate specific logic

Run:
    python main.py [--flags]
"""
from __future__ import annotations

import os
import argparse
import platform
import queue
import signal
import subprocess
import sys
import time
import json
import urllib.error
import urllib.request
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
OVERLAY_PERSIST_SEC = 0.25  # Smooth transient detector misses in the on-screen overlay


def _log_system_diagnostics():
    """Log hardware/OS diagnostics at startup for debugging shutdown issues."""
    print(f"[DIAG] Platform: {platform.system()} {platform.machine()}")
    print(f"[DIAG] Python: {sys.version}")
    if platform.system() == 'Linux':
        try:
            model = open('/proc/device-tree/model').read().strip('\x00\n')
            print(f"[DIAG] Board: {model}")
        except Exception:
            pass
        # Check GPU throttle state (Raspberry Pi specific)
        try:
            result = subprocess.run(
                ['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=5
            )
            print(f"[DIAG] Throttle: {result.stdout.strip()}")
        except Exception:
            pass
        # Log CMA pool size (relevant for Pi Camera + GPU memory issues)
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if 'CmaTotal' in line or 'CmaFree' in line:
                        print(f"[DIAG] {line.strip()}")
        except Exception:
            pass


def parse_args():
    p = argparse.ArgumentParser(description="TrueVision runtime")
    # Camera flags
    p.add_argument('--camera-width', type=int, default=640)
    p.add_argument('--camera-height', type=int, default=480)
    p.add_argument('--camera-fps', type=int, default=30)
    # Face recognizer flags
    p.add_argument('--face-detector', default=os.environ.get('FACE_DETECTOR', 'auto'), choices=['auto','hog','cnn'])
    p.add_argument('--match-threshold', type=float, default=0.6)
    p.add_argument('--quality-min-var', type=float, default=120.0)
    p.add_argument('--diversity-min-dist', type=float, default=0.20)
    p.add_argument('--add-cooldown-sec', type=float, default=5.0)
    p.add_argument('--bootstrap-template-count', type=int, default=5,
                   help='How many templates to collect before switching to steady-state template thresholds')
    p.add_argument('--bootstrap-force-until-count', type=int, default=3,
                   help='Collect templates on quality and cooldown alone until this many templates exist')
    p.add_argument('--bootstrap-quality-min-var', type=float, default=60.0,
                   help='Minimum Laplacian variance for bootstrap template collection')
    p.add_argument('--bootstrap-diversity-min-dist', type=float, default=0.08,
                   help='Minimum L2 distance between bootstrap templates once force-bootstrap is complete')
    p.add_argument('--bootstrap-add-cooldown-sec', type=float, default=0.75,
                   help='Cooldown between bootstrap template additions')
    p.add_argument('--template-verbose', action='store_true', help='Verbose logs for template add/skip decisions')
    p.add_argument('--absence-grace-sec', type=float, default=2.0)
    # Transcription flags
    p.add_argument('--whisper-model', default=os.environ.get('WHISPER_MODEL', 'tiny'))
    p.add_argument('--caption-interval', type=float, default=0.7, help='Seconds between caption updates')
    p.add_argument('--caption-max-words', type=int, default=30)
    p.add_argument('--caption-max-lines', type=int, default=2)
    p.add_argument('--caption-window-sec', type=float, default=2.0,
                   help='Rolling audio window to transcribe for live captions (default: %(default)s)')
    p.add_argument('--caption-language', default='en',
                   help='Language hint for fast local live captions; use auto to let Whisper detect language')
    p.add_argument('--show-caption-status', action='store_true',
                   help='Show debug caption status messages while waiting for speech-to-text output')
        # ESP32 UART flags
    p.add_argument('--serial-port', default='/dev/serial0', help='Serial port for ESP32 audio (default: /dev/serial0)')
    p.add_argument('--serial-baud', type=int, default=921600, help='Baud rate for ESP32 serial')
    p.add_argument('--force-mode', choices=['audio', 'face', 'both'],
                    help='Force runtime mode from the Pi side and ignore ESP32 mode packets. '
                        'BOTH is only effective while the server connection is available.')
    # UI/overlay flags
    p.add_argument('--overlay-only', action='store_true', default=OVERLAY_ONLY_DEFAULT)
    # Summary flags
    p.add_argument('--summary-async', action='store_true', help='Compute meeting summaries in a background thread')
    p.add_argument('--summary-max-sentences', type=int, default=1)
    p.add_argument('--summary-max-chars', type=int, default=140, help='Clamp 1-sentence summaries to this many chars (default: %(default)s)')
    p.add_argument('--prev-summary-max-chars', type=int, default=140, help='Clamp displayed previous-conversation summary (default: %(default)s)')
    # Server offloading flags
    p.add_argument('--server-url', default=os.environ.get('TRUEVISION_SERVER_URL', ''),
                   help='TrueVision server URL (e.g. http://192.168.1.100:8008). '
                        'Enables dual-mode: face recognition on Pi + audio on server.')
    p.add_argument('--no-server', action='store_true',
                   help='Disable server connection entirely (force local-only mode)')
    p.add_argument('--server-check-interval', type=float, default=5.0,
                   help='Seconds between server availability re-checks (default: %(default)s)')
    p.add_argument('--server-connect-retries', type=int, default=3,
                   help='How many startup connection attempts to make before reporting no server found')
    p.add_argument('--server-retry-delay', type=float, default=2.0,
                   help='Seconds to wait between startup server connection attempts')
    return p.parse_args()


def recognize_face():
    args = parse_args()
    window_name = "Face Recognition"

    _log_system_diagnostics()

    # Database connection (deferred from module level to avoid I/O before arg parsing)
    conn = open_db()
    cursor = conn.cursor()

    cap = open_camera(args.camera_width, args.camera_height)
    if cap is None:
        print("ERROR: Could not open a camera backend. Check the Pi camera or your desktop webcam.")
        return
    print("Press 'q' to quit.")

    if args.overlay_only:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    presence_state = {}
    last_detected_ts = {}
    ABSENCE_GRACE_SEC = args.absence_grace_sec
    last_overlay_draw_items = []
    last_overlay_draw_ts = 0.0

    recognizer_cfg = RecognizerConfig(
        models_dir=MODELS_DIR,
        detector_mode=args.face_detector,
        match_threshold=args.match_threshold,
        quality_min_var=args.quality_min_var,
        diversity_min_dist=args.diversity_min_dist,
        add_cooldown_sec=args.add_cooldown_sec,
        bootstrap_template_count=args.bootstrap_template_count,
        bootstrap_force_until_count=args.bootstrap_force_until_count,
        bootstrap_quality_min_var=args.bootstrap_quality_min_var,
        bootstrap_diversity_min_dist=args.bootstrap_diversity_min_dist,
        bootstrap_add_cooldown_sec=args.bootstrap_add_cooldown_sec,
        verbose=bool(getattr(args, 'template_verbose', False)),
    )
    recog: Optional[Recognizer] = None

    def _get_recognizer() -> Optional[Recognizer]:
        nonlocal recog
        if recog is not None:
            return recog
        try:
            print("Face recognition: Initializing models")
            recog = Recognizer(recognizer_cfg)
            return recog
        except Exception as e:
            print(f"WARNING: Face recognition unavailable ({e}).")
            return None

    active_recorders = {}
    active_meetings = {}
    create_recorder = None
    Transcriber = None
    def summarize_text(txt: str, max_sentences: int = 5) -> str:
        return ''
    try:
        from audio_analysis.transcription import (
            create_recorder as _create_recorder,
            Transcriber as _Transcriber,
            summarize_text as _summarize_text,
        )
        create_recorder, Transcriber, summarize_text = _create_recorder, _Transcriber, _summarize_text
    except Exception as e:
        print(f"WARNING: Transcription modules unavailable ({e}). Transcription disabled.")

    # ── ESP32 mode switch and marker integration ──────────────────────────────
    # current_mode is a one-element list so the closures below can mutate it.
    from audio_analysis.esp32_serial_audio import MODE_AUDIO, MODE_FACE
    MODE_BOTH = 0x02  # Pi-side only; ESP32 no longer sends this
    forced_mode = {
        'audio': MODE_AUDIO,
        'face': MODE_FACE,
        'both': MODE_BOTH,
    }.get(args.force_mode)
    _server_offload_active = False  # True when server is handling audio
    current_mode = [forced_mode if forced_mode is not None else MODE_FACE]
    last_single_mode = [forced_mode if forced_mode in (MODE_AUDIO, MODE_FACE) else MODE_FACE]
    AUDIO_SESSION_KEY = -1
    # Thread-safe queue for marker events (PKT_MARKER from ESP32 button).
    # The main loop drains the queue and appends [MARKER HH:MM:SS] to the
    # active meeting transcript.
    _marker_queue: queue.Queue = queue.Queue()

    def _mode_label(mode_byte: int) -> str:
        if mode_byte == MODE_FACE:
            return "FACE"
        if mode_byte == MODE_AUDIO:
            return "AUDIO"
        return "BOTH"

    def _effective_mode(mode_byte: int) -> int:
        if forced_mode in (MODE_AUDIO, MODE_FACE, MODE_BOTH):
            if forced_mode == MODE_BOTH and not _server_offload_active:
                return last_single_mode[0]
            return forced_mode
        if _server_offload_active:
            return MODE_BOTH
        if mode_byte in (MODE_AUDIO, MODE_FACE):
            return mode_byte
        return last_single_mode[0]

    _first_mode_received = [False]

    def _set_requested_mode(mode_byte: int, *, source: str) -> None:
        if current_mode[0] == mode_byte and _first_mode_received[0]:
            return
        _first_mode_received[0] = True
        current_mode[0] = mode_byte
        effective_mode = _effective_mode(mode_byte)
        if mode_byte in (MODE_AUDIO, MODE_FACE):
            last_single_mode[0] = mode_byte
            if effective_mode == MODE_BOTH:
                print(
                    f"{source}: Switch requested {_mode_label(mode_byte)}; "
                    "server available so effective mode is BOTH"
                )
            else:
                print(f"{source}: Mode changed to {_mode_label(mode_byte)}")
            return

        if effective_mode == MODE_BOTH:
            print(f"{source}: Mode changed to BOTH")
        else:
            print(
                f"{source}: Mode changed to BOTH, but server is unavailable; "
                f"using {_mode_label(effective_mode)}"
            )

    def _on_mode_change(mode_byte: int) -> None:
        if forced_mode is not None:
            print(f"ESP32: Mode packet {_mode_label(mode_byte)} ignored (forced {_mode_label(forced_mode)})")
            return
        _set_requested_mode(mode_byte, source="ESP32")

    def _on_marker() -> None:
        print("ESP32 BUTTON → KILLING PROCESS")
        import os
        import signal
        os.kill(os.getpid(), signal.SIGTERM)

    # Wire up callbacks on the shared receiver for ESP32 mode/marker events.
    _esp32_receiver = None
    esp32_port_exists = bool(args.serial_port) and os.path.exists(args.serial_port)
    if esp32_port_exists:
        try:
            from audio_analysis.transcription import get_shared_receiver
            _esp32_receiver = get_shared_receiver(
                serial_port=args.serial_port,
                serial_baud=args.serial_baud,
                on_mode_change=_on_mode_change,
                on_marker=_on_marker,
            )
            if forced_mode is not None:
                print(f"ESP32: Pi-side forced mode {_mode_label(forced_mode)}")
            print(f"ESP32: Receiver initialised on {args.serial_port}")
        except Exception as _recv_err:
            print(f"WARNING: ESP32 not detected on {args.serial_port} — running without ESP32 integration")
    else:
        print(f"ESP32: Serial port {args.serial_port} not present; integration disabled")

    transcriber: Optional[object] = None
    if Transcriber is not None:
        try:
            transcriber = Transcriber(model_size=args.whisper_model)
        except Exception as e:
            print(f"WARNING: Transcriber initialization failed ({e}). Transcription disabled.")

    captioner = None
    if transcriber is not None:
        from audio_analysis.live_caption import LiveCaptioner, CaptionConfig

        captioner = LiveCaptioner(
            transcriber,
            CaptionConfig(
                interval_sec=args.caption_interval,
                max_words=args.caption_max_words,
                window_sec=args.caption_window_sec,
                language=args.caption_language,
            ),
        )

    # ── Server connection for offloaded transcription ─────────────────────
    server_conn = None
    audio_forwarder = None
    _last_server_check = 0.0

    if not getattr(args, 'no_server', False):
        server_url = getattr(args, 'server_url', '') or ''
        try:
            from audio_analysis.server_connection import ServerConnection
            server_conn = ServerConnection(
                url=server_url or None,
                check_interval_sec=getattr(args, 'server_check_interval', 5.0),
                timeout_sec=3.0,
            )
            # Do a synchronous initial check with a few quick retries so make run
            # reports a clear startup result before falling back to local mode.
            if server_conn.check_with_retries(
                attempts=max(1, int(getattr(args, 'server_connect_retries', 3))),
                delay_sec=max(0.0, float(getattr(args, 'server_retry_delay', 2.0))),
            ):
                print(f"Server available: {server_conn.url}")
                _server_offload_active = True
            else:
                configured_url = server_url or os.environ.get('TRUEVISION_SERVER_URL', '')
                if configured_url:
                    print(
                        f"No server found at {configured_url} after "
                        f"{max(1, int(getattr(args, 'server_connect_retries', 3)))} attempts "
                        "— running in local-only mode"
                    )
                else:
                    print(
                        f"No server found after {max(1, int(getattr(args, 'server_connect_retries', 3)))} attempts "
                        "(checked configured URL/mDNS) — running in local-only mode"
                    )
            server_conn.start()
        except Exception as e:
            print(f"WARNING: Server connection init failed ({e}). Running local-only.")
            server_conn = None

    def _ensure_audio_forwarder():
        """Create the AudioForwarder if server is available and ESP32 receiver exists."""
        nonlocal audio_forwarder
        if audio_forwarder is not None:
            return audio_forwarder
        if server_conn is None or not server_conn.is_available:
            return None
        if _esp32_receiver is None:
            return None
        try:
            from audio_analysis.audio_forwarder import AudioForwarder
            ws_url = server_conn.ws_url
            if ws_url is None:
                return None
            audio_forwarder = AudioForwarder(ws_url, _esp32_receiver)
            audio_forwarder.start()
            print(f"AudioForwarder: Connected to {ws_url}")
            return audio_forwarder
        except Exception as e:
            print(f"WARNING: AudioForwarder init failed ({e}).")
            return None

    def _remote_summarizer_cfg():
        if server_conn is None or not server_conn.url:
            return None
        try:
            from summarization.remote_client import RemoteSummarizerConfig
            return RemoteSummarizerConfig(url=server_conn.url)
        except Exception:
            return None

    def _fetch_server_meeting_result(meeting_id: int) -> Optional[dict]:
        if server_conn is None or not server_conn.url:
            return None
        req = urllib.request.Request(
            f"{server_conn.url}/api/meetings/{int(meeting_id)}/status",
            method='GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                payload = json.loads(resp.read().decode('utf-8', errors='replace'))
        except (urllib.error.URLError, ValueError, OSError):
            return None
        if payload.get('status') != 'done':
            return None
        return {
            'meeting_id': payload.get('meeting_id'),
            'transcript': payload.get('transcript', '') or '',
            'summary': payload.get('summary', '') or '',
        }

    def _start_session(session_key: int, *, person_id: Optional[int], label_prefix: str) -> None:
        if create_recorder is None or session_key in active_recorders:
            return
        rec = create_recorder(
            serial_port=args.serial_port,
            serial_baud=args.serial_baud,
        )
        try:
            audio_path_pending = rec.start(RECORDINGS_DIR, label_prefix)
        except Exception as _rec_err:
            print(f"WARNING: Could not start audio recorder ({_rec_err}).")
            return

        active_recorders[session_key] = rec
        if captioner is not None:
            captioner.clear(session_key)

        meeting_id_val = None
        if person_id is not None:
            cursor.execute(
                "INSERT INTO meetings (person_id, started_at, audio_path) VALUES (?, datetime('now'), ?)",
                (person_id, audio_path_pending),
            )
            meeting_id_val = cursor.lastrowid
            active_meetings[session_key] = meeting_id_val
            conn.commit()

        # If server offload is active, signal the forwarder to start streaming
        if _server_offload_active:
            fwd = _ensure_audio_forwarder()
            if fwd is not None and fwd.is_connected:
                fwd.send_session_start(session_key, person_id=person_id,
                                       meeting_id=meeting_id_val)

    TELEGRAM_DGX_URL = "http://10.186.71.82:8008"

    def _is_assistant_command(text: str) -> bool:
        lower = (text or "").lower()
        return "assistant" in lower or "truevision" in lower

    def _clean_assistant_command(text: str) -> str:
        text = text or ""
        lower = text.lower()
        idx_assistant = lower.find("assistant")
        idx_truevision = lower.find("truevision")
        if idx_assistant == -1 and idx_truevision == -1:
            return text.strip()

        valid_idxs = [i for i in (idx_assistant, idx_truevision) if i != -1]
        idx = min(valid_idxs)
        keyword = "assistant" if idx == idx_assistant else "truevision"
        return text[idx + len(keyword):].strip(" ,.")

    def _maybe_send_telegram_command(transcript_text: str) -> bool:
        transcript_text = (transcript_text or "").strip()
        print("FULL TRANSCRIPT:", transcript_text)

        if not _is_assistant_command(transcript_text):
            print("No Assistant wake word detected.")
            return False

        command = _clean_assistant_command(transcript_text)
        print("CLEANED ASSISTANT COMMAND:", command)

        if not command:
            print("Assistant command detected, but command was empty.")
            return True

        payload = json.dumps({"command": command}).encode("utf-8")

        req = urllib.request.Request(
            f"{TELEGRAM_DGX_URL}/telegram_llm",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                response_text = resp.read().decode("utf-8", errors="replace")
                print("Telegram response:", response_text)
        except Exception as e:
            print(f"Telegram send failed: {e}")

        return True

    def _stop_session(session_key: int) -> None:
        rec = active_recorders.pop(session_key, None)
        meeting_id = active_meetings.pop(session_key, None)
        if rec is None:
            if captioner is not None:
                captioner.clear(session_key)
            return

        audio_path_final = rec.stop()
        if captioner is not None:
            captioner.clear(session_key)

        if meeting_id is None:
            transcript_text = ""

            if audio_path_final and transcriber is not None:
                try:
                    transcript_text = transcriber.transcribe(audio_path_final)
                    print(f"Audio-only transcript: {transcript_text}")
                except Exception as e:
                    print(f"Audio-only transcription failed: {e}")

            if transcript_text:
                _maybe_send_telegram_command(transcript_text)

            if audio_path_final:
                print(f"Audio-only session saved to {audio_path_final}")

            return

        # ── Server-offloaded transcription path ──────────────────────────
        if _server_offload_active and audio_forwarder is not None and audio_forwarder.is_connected:
            # Signal session end to server — it will do final transcription + summarization
            prev_summary = ""
            person_name = None
            try:
                cursor.execute(
                    """SELECT COALESCE(summary,'') FROM meetings
                       WHERE person_id = ? AND ended_at IS NOT NULL AND id < ?
                             AND COALESCE(summary,'') != ''
                       ORDER BY id DESC LIMIT 1""",
                    (int(session_key), int(meeting_id)),
                )
                rprev = cursor.fetchone()
                prev_summary = (rprev[0] if rprev else "") or ""
                cursor.execute("SELECT COALESCE(name,'') FROM faces WHERE id = ?", (int(session_key),))
                rname = cursor.fetchone()
                person_name = (rname[0] if rname else "") or None
            except Exception:
                pass

            audio_forwarder.send_session_end(
                session_key,
                previous_summary=prev_summary,
                person_name=person_name,
                max_chars=int(getattr(args, 'summary_max_chars', 140)),
            )

            # Mark meeting as ended; transcript/summary will be filled by the
            # result-polling thread below.
            cursor.execute(
                "UPDATE meetings SET ended_at = datetime('now') WHERE id = ?",
                (meeting_id,),
            )
            conn.commit()

            # Poll for server result in a background thread
            import threading as _threading

            def _poll_server_result(mid: int, sk: int, db_path: str):
                try:
                    from data_access.db import open_db as _open_db
                    import time as _time
                    # Wait up to 30s for the server to respond via the WebSocket result
                    for _ in range(60):
                        result = audio_forwarder.get_result(sk)
                        if result is None:
                            result = _fetch_server_meeting_result(mid)
                        if result is not None:
                            c2 = _open_db(db_path)
                            c2.execute(
                                "UPDATE meetings SET transcript = ?, summary = ? WHERE id = ?",
                                (result.get("transcript", ""), result.get("summary", ""), mid),
                            )
                            c2.commit()
                            c2.close()
                            print(f"Server result saved for meeting {mid}")
                            return
                        _time.sleep(0.5)
                    print(f"WARNING: Timed out waiting for server result for meeting {mid}")
                except Exception as _e:
                    print(f"Server result poll failed for meeting {mid}: {_e}")

            _t = _threading.Thread(target=_poll_server_result,
                                   args=(meeting_id, session_key, DB_PATH_DEFAULT),
                                   daemon=True)
            _t.start()
            if audio_forwarder is not None:
                audio_forwarder.clear(session_key)
            return

        # ── Local transcription path (original behavior) ────────────────

        transcript_text = None
        if audio_path_final and transcriber is not None:
            try:
                transcript_text = transcriber.transcribe(audio_path_final)
            except Exception as e:
                print(f"Transcription failed: {e}")
                transcript_text = None

        if transcript_text is None:
            cursor.execute("SELECT COALESCE(transcript,'') FROM meetings WHERE id = ?", (meeting_id,))
            (existing_transcript,) = cursor.fetchone() or ('',)
            transcript_text = existing_transcript or ''

        if transcript_text:
            _maybe_send_telegram_command(transcript_text)

        cursor.execute(
            "UPDATE meetings SET ended_at = datetime('now'), transcript = ? WHERE id = ?",
            (transcript_text, meeting_id),
        )
        conn.commit()

        if getattr(args, 'summary_async', False):
            import threading

            def _bg_summarize(mid: int, db_path: str, text: str, max_sent: int, max_chars: int):
                try:
                    from data_access.db import open_db as _open_db
                    from audio_analysis.transcription import summarize_one_sentence as _summ1
                    from audio_analysis.transcription import summarize_text as _summ
                    try:
                        from summarization.remote_client import remote_summarize_one_sentence as _remote
                    except Exception:
                        _remote = None
                    c2 = _open_db(db_path)
                    cur2 = c2.cursor()

                    summary = ""
                    if _remote is not None:
                        try:
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
                                cfg=_remote_summarizer_cfg(),
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
            )
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
                            cursor.execute(
                                "SELECT COALESCE(name,'') FROM faces WHERE id = ?",
                                (int(session_key),),
                            )
                            rname = cursor.fetchone()
                            person_name = (rname[0] if rname else "") or None

                            cursor.execute(
                                """
                                SELECT COALESCE(summary,'') FROM meetings
                                WHERE person_id = ? AND ended_at IS NOT NULL AND id < ? AND COALESCE(summary,'') != ''
                                ORDER BY id DESC LIMIT 1
                                """,
                                (int(session_key), int(meeting_id)),
                            )
                            rprev = cursor.fetchone()
                            prev_summary = (rprev[0] if rprev else "") or ""

                            summary_text = remote_summarize_one_sentence(
                                transcript=transcript_text,
                                previous_summary=prev_summary,
                                person_name=person_name,
                                max_chars=int(getattr(args, 'summary_max_chars', 140)),
                                cfg=_remote_summarizer_cfg(),
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

    def _clear_face_presence() -> None:
        for pid in list(presence_state.keys()):
            if pid != AUDIO_SESSION_KEY:
                presence_state.pop(pid, None)
        last_detected_ts.clear()
        if hasattr(recognize_face, "_prev_summaries"):
            recognize_face._prev_summaries.clear()  # type: ignore[attr-defined]

    def _apply_mode_transition(mode_byte: int) -> None:
        if mode_byte == MODE_AUDIO:
            print("Mode: Switching to AUDIO — face recognition paused, live captions enabled")
            for session_key in list(active_recorders.keys()):
                _stop_session(session_key)
            _clear_face_presence()
            if create_recorder is not None:
                _start_session(AUDIO_SESSION_KEY, person_id=None, label_prefix="audio_only")
                if AUDIO_SESSION_KEY in active_recorders:
                    presence_state[AUDIO_SESSION_KEY] = 'present'
        elif mode_byte == MODE_FACE:
            print("Mode: Switching to FACE — face recognition active, audio recording stopped")
            for session_key in list(active_recorders.keys()):
                _stop_session(session_key)
            presence_state.clear()
            last_detected_ts.clear()
            if captioner is not None:
                captioner.clear_all()
        else:
            print("Mode: Switching to BOTH — face recognition + server audio active")
            if AUDIO_SESSION_KEY in active_recorders:
                _stop_session(AUDIO_SESSION_KEY)
            presence_state.pop(AUDIO_SESSION_KEY, None)
            last_detected_ts.pop(AUDIO_SESSION_KEY, None)
            if captioner is not None:
                captioner.clear(AUDIO_SESSION_KEY)

    applied_mode = None
    print(f"Starting in {_mode_label(_effective_mode(current_mode[0]))} mode")

    while True:
        effective_mode = _effective_mode(current_mode[0])
        if applied_mode != effective_mode:
            _apply_mode_transition(effective_mode)
            applied_mode = effective_mode

        # ── Periodic server availability check ────────────────────────────
        if server_conn is not None:
            now_check = time.time()
            if (now_check - _last_server_check) > getattr(args, 'server_check_interval', 30.0):
                _last_server_check = now_check
                was_active = _server_offload_active
                _server_offload_active = server_conn.is_available

                if _server_offload_active and not was_active:
                    print("Server became available — enabling dual-mode (face + server audio)")
                    _ensure_audio_forwarder()
                    # Re-apply mode to allow BOTH when server handles audio
                    applied_mode = None
                elif not _server_offload_active and was_active:
                    print("Server became unavailable — falling back to local-only mode")
                    if audio_forwarder is not None:
                        audio_forwarder.stop()
                        audio_forwarder = None
                    # Re-apply mode so ESP32 gating takes effect again
                    applied_mode = None
                elif (
                    _server_offload_active
                    and audio_forwarder is not None
                    and getattr(audio_forwarder, 'retry_exhausted', False)
                ):
                    print("AudioForwarder: reconnect attempts exhausted — retrying on next health cycle")
                    audio_forwarder.stop()
                    audio_forwarder = None
                    _ensure_audio_forwarder()

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
        overlay_draw_items = []
        if effective_mode != MODE_AUDIO:
            recognizer = _get_recognizer()
            faces_info = recognizer.detect_and_recognize(conn, frame) if recognizer is not None else []
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

                        if recognizer is not None:
                            recognizer.update_seen(conn, recognized_id)
                        presence_state[recognized_id] = 'present'
                        if recognized_seen_count is not None:
                            recognized_seen_count += 1
                        recognized_last_seen_str = "now"
                        if create_recorder is not None and recognized_id not in active_recorders and effective_mode == MODE_BOTH:
                            _start_session(
                                int(recognized_id),
                                person_id=int(recognized_id),
                                label_prefix=f"person{recognized_id}",
                            )
                    else:
                        presence_state[recognized_id] = 'present'

                    if recognizer is not None and info.embedding is not None:
                        if recognizer.maybe_add_embedding(conn, recognized_id, info.embedding, info.quality):
                            prune_embeddings_if_needed(conn, recognized_id, MAX_TEMPLATES_PER_PERSON)

                label = recognized_name
                if recognized_id is not None and recognized_seen_count is not None:
                    label = f"{recognized_name} (seen {recognized_seen_count})"
                last_label = None
                prev_text = None
                show_rec = False
                if recognized_id is not None:
                    last_label = f"Last: {recognized_last_seen_str if recognized_last_seen_str else '—'}"
                    prev_summary = prev_summaries.get(int(recognized_id), "")
                    if prev_summary:
                        prev_line = prev_summary
                        max_chars = 48
                        if len(prev_line) > max_chars:
                            prev_line = prev_line[: max_chars - 1].rstrip() + "…"
                        prev_text = f"Prev: {prev_line}"
                    if recognized_id in active_recorders and effective_mode == MODE_BOTH:
                        show_rec = True

                overlay_draw_items.append(
                    {
                        'rect': (x, y, w, h),
                        'label': label,
                        'last_label': last_label,
                        'prev_text': prev_text,
                        'show_rec': show_rec,
                    }
                )

        if overlay_draw_items:
            last_overlay_draw_items = [dict(item) for item in overlay_draw_items]
            last_overlay_draw_ts = time.time()
        elif last_overlay_draw_items and (time.time() - last_overlay_draw_ts) <= OVERLAY_PERSIST_SEC:
            overlay_draw_items = last_overlay_draw_items

        for item in overlay_draw_items:
            x, y, w, h = item['rect']
            cv2.rectangle(display_frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
            cv2.putText(display_frame, item['label'], (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if item['last_label']:
                cv2.putText(display_frame, item['last_label'], (x, y+15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if item['prev_text']:
                cv2.putText(display_frame, item['prev_text'], (x, y+30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
            if item['show_rec']:
                cv2.putText(display_frame, "REC", (x, y+45), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        # Live transcription overlay (closed captioning)
        caption = None
        caption_status = None
        if effective_mode != MODE_FACE:
            if _server_offload_active and audio_forwarder is not None and audio_forwarder.is_connected:
                # Get caption from server via WebSocket
                for pid in list(active_recorders.keys()):
                    caption = audio_forwarder.get_latest_caption(pid)
                    if caption:
                        break
            elif captioner is not None and active_recorders:
                captioner.update(active_recorders, active_meetings, cursor)
                caption = captioner.get_caption_for_present(presence_state)
                caption_status = captioner.get_status_for_present(presence_state)

        overlay_text = caption
        if overlay_text is None and args.show_caption_status:
            overlay_text = caption_status
        if overlay_text:
            img_h, img_w = display_frame.shape[0], display_frame.shape[1]
            margin = 10
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.6
            thickness = 2
            words = overlay_text.split()
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

        now_ts = time.time()
        for pid, state in list(presence_state.items()):
            if pid == AUDIO_SESSION_KEY:
                continue
            if state == 'present' and pid not in recognized_ids_in_frame:
                last_ts = last_detected_ts.get(pid)
                if last_ts is not None and (now_ts - last_ts) > ABSENCE_GRACE_SEC:
                    presence_state[pid] = 'absent'
                    if pid in active_recorders:
                        _stop_session(pid)

        cv2.imshow(window_name, display_frame)
        key = cv2.waitKey(1)
        if key == ord('q'):
            break

    try:
        cap.release()
    except Exception:
        pass
    for session_key in list(active_recorders.keys()):
        try:
            _stop_session(session_key)
        except Exception as stop_err:
            print(f"WARNING: Failed to finalize recording for session {session_key}: {stop_err}")
    cv2.destroyAllWindows()
    try:
        if audio_forwarder is not None:
            audio_forwarder.stop()
    except Exception:
        pass
    try:
        if captioner is not None:
            captioner.stop()
    except Exception:
        pass
    try:
        if server_conn is not None:
            server_conn.stop()
    except Exception:
        pass


if __name__ == "__main__":
    # Graceful shutdown on SIGTERM (e.g. systemd stop)
    signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))
    try:
        recognize_face()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        print(f"[FATAL] Unhandled exception: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
