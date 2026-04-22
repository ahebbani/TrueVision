"""WebSocket audio forwarder — streams ESP32 serial audio to the server
and receives live captions / transcription results back.

Runs in a background thread.  The main loop reads captions via
``get_latest_caption()`` and final results via ``get_result()``.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, Optional, Tuple

try:
    from websocket import WebSocketApp, WebSocketException  # websocket-client
except ImportError:
    WebSocketApp = None  # type: ignore[assignment,misc]
    WebSocketException = Exception  # type: ignore[assignment,misc]


class AudioForwarder:
    """Forward audio from an ``ESP32SerialAudioReceiver`` to the TrueVision
    server over WebSocket, and receive live captions back."""

    # How often (seconds) to read from the serial receiver and forward
    FORWARD_INTERVAL = 0.032  # ~32 ms = one ESP32 audio packet
    RECONNECT_ATTEMPTS = 3
    RECONNECT_BACKOFF_SEC = 2.0

    def __init__(self, ws_url: str, serial_receiver):
        """
        Args:
            ws_url: WebSocket URL, e.g. ``ws://192.168.1.100:8008/ws/audio``
            serial_receiver: An ``ESP32SerialAudioReceiver`` instance.
        """
        if WebSocketApp is None:
            raise RuntimeError(
                "websocket-client is not installed. Install with: pip install websocket-client"
            )
        self._ws_url = ws_url
        self._receiver = serial_receiver

        self._ws: Optional[WebSocketApp] = None  # type: ignore[assignment]
        self._thread: Optional[threading.Thread] = None
        self._forward_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()

        # State
        self._lock = threading.Lock()
        self._captions: Dict[int, str] = {}
        self._results: Dict[int, dict] = {}  # session_key → {transcript, summary, meeting_id}
        self._last_read_pos = 0  # bytes already forwarded from receiver buffer
        self._reconnect_failures = 0
        self._retry_exhausted = False

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def retry_exhausted(self) -> bool:
        return self._retry_exhausted

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._retry_exhausted = False
        self._reconnect_failures = 0
        self._thread = threading.Thread(target=self._ws_run, daemon=True,
                                        name="audio-fwd-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._connected.clear()
        self._thread = None
        self._forward_thread = None

    def send_session_start(self, session_key: int, person_id: Optional[int] = None,
                           meeting_id: Optional[int] = None) -> None:
        self._send_json({
            "type": "session_start",
            "session_key": session_key,
            "person_id": person_id,
            "meeting_id": meeting_id,
        })
        # Reset read position so we forward fresh audio
        self._last_read_pos = len(self._receiver.get_all_audio())

    def send_session_end(self, session_key: int,
                         previous_summary: str = "",
                         person_name: Optional[str] = None,
                         max_chars: int = 140) -> None:
        self._send_json({
            "type": "session_end",
            "session_key": session_key,
            "previous_summary": previous_summary,
            "person_name": person_name,
            "max_chars": max_chars,
        })

    def get_latest_caption(self, session_key: int) -> Optional[str]:
        with self._lock:
            return self._captions.get(session_key)

    def get_result(self, session_key: int) -> Optional[dict]:
        """Pop the final result for a session (transcript + summary)."""
        with self._lock:
            return self._results.pop(session_key, None)

    def clear(self, session_key: int) -> None:
        with self._lock:
            self._captions.pop(session_key, None)
            self._results.pop(session_key, None)

    # ── Internal: WebSocket lifecycle ────────────────────────────────────

    def _ws_run(self) -> None:
        """Connect/reconnect loop."""
        while not self._stop.is_set():
            try:
                self._ws = WebSocketApp(
                    self._ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as e:
                print(f"[audio_forwarder] WebSocket error: {e}")
            self._connected.clear()
            if not self._stop.is_set():
                self._reconnect_failures += 1
                if self._reconnect_failures >= self.RECONNECT_ATTEMPTS:
                    self._retry_exhausted = True
                    print(
                        "[audio_forwarder] Reconnect attempts exhausted; "
                        "waiting for the next server health cycle"
                    )
                    break
                print(
                    f"[audio_forwarder] Reconnect attempt "
                    f"{self._reconnect_failures + 1}/{self.RECONNECT_ATTEMPTS} "
                    f"in {self.RECONNECT_BACKOFF_SEC:.1f}s"
                )
                time.sleep(self.RECONNECT_BACKOFF_SEC)

    def _on_open(self, ws) -> None:
        self._connected.set()
        self._reconnect_failures = 0
        self._retry_exhausted = False
        print(f"[audio_forwarder] Connected to {self._ws_url}")
        # Start the audio forwarding thread
        self._forward_thread = threading.Thread(
            target=self._forward_loop, daemon=True, name="audio-fwd-pcm"
        )
        self._forward_thread.start()

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type", "")
        sk = int(data.get("session_key", 0))

        if msg_type == "caption":
            with self._lock:
                self._captions[sk] = data.get("text", "")

        elif msg_type == "result":
            with self._lock:
                self._results[sk] = {
                    "meeting_id": data.get("meeting_id"),
                    "transcript": data.get("transcript", ""),
                    "summary": data.get("summary", ""),
                }

    def _on_error(self, ws, error) -> None:
        print(f"[audio_forwarder] WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected.clear()
        print(f"[audio_forwarder] Disconnected from server")

    # ── Internal: audio forwarding ───────────────────────────────────────

    def _forward_loop(self) -> None:
        """Continuously read new audio from the serial receiver and send
        it as binary WebSocket frames."""
        while not self._stop.is_set() and self._connected.is_set():
            try:
                all_audio = self._receiver.get_all_audio()
                new_len = len(all_audio)
                if new_len > self._last_read_pos:
                    chunk = all_audio[self._last_read_pos:]
                    self._last_read_pos = new_len
                    if self._ws is not None and self._connected.is_set():
                        try:
                            self._ws.send(chunk, opcode=0x2)  # binary
                        except Exception:
                            break
            except Exception:
                break
            time.sleep(self.FORWARD_INTERVAL)

    def _send_json(self, obj: dict) -> None:
        if self._ws is not None and self._connected.is_set():
            try:
                self._ws.send(json.dumps(obj))
            except Exception as e:
                print(f"[audio_forwarder] Failed to send JSON: {e}")
