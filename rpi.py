import os
import cv2
import json
import logging
import platform
import time
import wave
import queue
import serial
import threading
import subprocess
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

import numpy as np
import requests
import websocket
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn


logger = logging.getLogger(__name__)


# =========================
# Config
# =========================

DGX_HTTP_URL = "http://10.186.78.209:8008"
DGX_AUDIO_WS_URL = "ws://10.186.78.209:8008/ws/audio"
DGX_FACE_WS_URL = "ws://10.186.78.209:8008/ws/face"

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/serial0")
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "921600"))

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2

AUDIO_CHUNK_SECONDS = float(os.getenv("AUDIO_CHUNK_SECONDS", "2.5"))
AUDIO_CHUNK_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH_BYTES * AUDIO_CHUNK_SECONDS)

FACE_SEND_INTERVAL_SECONDS = float(os.getenv("FACE_SEND_INTERVAL_SECONDS", "0.35"))
FACE_SEND_WIDTH = int(os.getenv("FACE_SEND_WIDTH", "640"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "70"))

DISPLAY_WIDTH = int(os.getenv("DISPLAY_WIDTH", "1280"))
DISPLAY_HEIGHT = int(os.getenv("DISPLAY_HEIGHT", "720"))
HUD_SHOW_CAMERA_BG = os.getenv("HUD_SHOW_CAMERA_BG", "0").lower() in {"1", "true", "yes", "on"}
HUD_WINDOW_NAME = os.getenv("HUD_WINDOW_NAME", "TrueVision HUD")

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")


# =========================
# ESP32 Packet Protocol
# =========================

SYNC_1 = 0xAA
SYNC_2 = 0x55

PKT_AUDIO = 0x01
PKT_MODE_CHANGE = 0x02

MODE_AUDIO = 0x00
MODE_FACE = 0x01
MODE_DUAL = 0x02

MODE_NAMES = {
    MODE_AUDIO: "AUDIO",
    MODE_FACE: "FACE",
    MODE_DUAL: "DUAL"
}

MODE_FROM_NAME = {
    "audio": MODE_AUDIO,
    "face": MODE_FACE,
    "dual": MODE_DUAL
}


# =========================
# Shared State
# =========================

state = {
    "running": True,
    "mode": MODE_FACE,

    "caption": "",
    "last_original_text": "",
    "last_language": "",
    "last_command": {},
    "summary": "",

    "faces": [],
    "known_faces": {},
    "last_face_error": "",

    "weather": "",
    "news": "",
    "location": None,
    "reminders": [],

    "dgx_audio_connected": False,
    "dgx_face_connected": False,
    "uart_connected": False,
}

audio_queue = queue.Queue(maxsize=300)
frame_queue = queue.Queue(maxsize=2)

state_lock = threading.Lock()


# =========================
# UART Reader
# =========================

class UARTPacketReader:
    def __init__(self, port: str, baud: int):
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=0.05
        )

    def read_packet(self) -> Optional[Dict[str, Any]]:
        while True:
            b = self.ser.read(1)

            if not b:
                return None

            if b[0] == SYNC_1:
                b2 = self.ser.read(1)

                if b2 and b2[0] == SYNC_2:
                    break

        header = self.ser.read(3)

        if len(header) != 3:
            return None

        pkt_type = header[0]
        data_len = header[1] | (header[2] << 8)

        if data_len < 0 or data_len > 4096:
            return None

        payload = self.ser.read(data_len)
        checksum_raw = self.ser.read(1)

        if len(payload) != data_len or len(checksum_raw) != 1:
            return None

        checksum = checksum_raw[0]
        calc = sum(payload) & 0xFF

        if checksum != calc:
            print("[PI] Bad checksum")
            return None

        return {
            "type": pkt_type,
            "payload": payload
        }


def uart_thread():
    reader = None

    while state["running"]:
        try:
            print(f"[PI] Opening UART {SERIAL_PORT} at {SERIAL_BAUD}")
            reader = UARTPacketReader(SERIAL_PORT, SERIAL_BAUD)

            with state_lock:
                state["uart_connected"] = True

            print("[PI] UART connected")
            break

        except Exception as e:
            print("[PI] UART failed:", e)

            with state_lock:
                state["uart_connected"] = False

            time.sleep(2)

    if reader is None:
        return

    while state["running"]:
        pkt = reader.read_packet()

        if pkt is None:
            continue

        pkt_type = pkt["type"]
        payload = pkt["payload"]

        if pkt_type == PKT_MODE_CHANGE and len(payload) >= 1:
            mode = payload[0]

            if mode in MODE_NAMES:
                set_mode_state(mode)

                print(f"[PI] ESP32 mode: {MODE_NAMES[mode]}")

        elif pkt_type == PKT_AUDIO:
            with state_lock:
                mode = state["mode"]

            if mode in [MODE_AUDIO, MODE_DUAL]:
                try:
                    audio_queue.put_nowait(payload)
                except queue.Full:
                    # Drop oldest audio if overloaded
                    try:
                        audio_queue.get_nowait()
                        audio_queue.put_nowait(payload)
                    except Exception:
                        pass


# =========================
# Audio to DGX
# =========================

def pcm_to_wav_bytes(pcm_bytes: bytes) -> bytes:
    import io

    buf = io.BytesIO()

    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)

    return buf.getvalue()


def dgx_audio_thread():
    pcm_buffer = bytearray()

    while state["running"]:
        try:
            print("[PI] Connecting audio WebSocket:", DGX_AUDIO_WS_URL)
            ws = websocket.create_connection(DGX_AUDIO_WS_URL, timeout=10)

            with state_lock:
                state["dgx_audio_connected"] = True

            print("[PI] DGX audio connected")

            while state["running"]:
                try:
                    pcm = audio_queue.get(timeout=0.2)
                    pcm_buffer.extend(pcm)
                except queue.Empty:
                    continue

                if len(pcm_buffer) >= AUDIO_CHUNK_BYTES:
                    chunk = bytes(pcm_buffer[:AUDIO_CHUNK_BYTES])
                    del pcm_buffer[:AUDIO_CHUNK_BYTES]

                    wav_bytes = pcm_to_wav_bytes(chunk)

                    ws.send_binary(wav_bytes)
                    response = ws.recv()

                    data = json.loads(response)

                    if "error" in data:
                        print("[PI] DGX audio error:", data["error"])
                        continue

                    text = data.get("text", "").strip()
                    original = data.get("original_text", "").strip()
                    language = data.get("detected_language", "")

                    if text:
                        with state_lock:
                            state["caption"] = text
                            state["last_original_text"] = original
                            state["last_language"] = language
                            state["last_command"] = data.get("command", {})

                        print("[CAPTION]", text)

        except Exception as e:
            print("[PI] Audio WebSocket failed:", e)

            with state_lock:
                state["dgx_audio_connected"] = False

            time.sleep(2)


# =========================
# Face frames to DGX
# =========================

def dgx_face_thread():
    while state["running"]:
        try:
            print("[PI] Connecting face WebSocket:", DGX_FACE_WS_URL)
            ws = websocket.create_connection(DGX_FACE_WS_URL, timeout=10)

            with state_lock:
                state["dgx_face_connected"] = True

            print("[PI] DGX face connected")

            while state["running"]:
                try:
                    jpeg_bytes = frame_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                ws.send_binary(jpeg_bytes)
                response = ws.recv()
                data = json.loads(response)

                faces = data.get("faces", [])

                with state_lock:
                    state["faces"] = faces
                    state["last_face_error"] = data.get("error", "")

                if faces:
                    print("[PI] Faces:", [f["name"] for f in faces])

        except Exception as e:
            print("[PI] Face WebSocket failed:", e)

            with state_lock:
                state["dgx_face_connected"] = False
                state["last_face_error"] = str(e)

            time.sleep(2)


# =========================
# HUD Display
# =========================

@dataclass
class Toast:
    text: str
    duration: float = 2.0
    created_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def is_expired(self) -> bool:
        return self.age > self.duration

    @property
    def alpha(self) -> float:
        fade_time = 0.5
        time_left = self.duration - self.age

        if time_left <= 0:
            return 0.0

        if time_left >= fade_time:
            return 1.0

        return time_left / fade_time


class ToastManager:
    def __init__(self):
        self._toasts: List[Toast] = []
        self._lock = threading.Lock()

    def show(self, text: str, duration: float = 2.0):
        clean_text = _trim_text(text, max_len=72)

        if not clean_text:
            return

        with self._lock:
            self._toasts.append(Toast(text=clean_text, duration=duration))

    def get_active_toasts(self) -> List[Toast]:
        with self._lock:
            self._toasts = [toast for toast in self._toasts if not toast.is_expired]
            return list(self._toasts)


def _trim_text(text: str, max_len: int = 56) -> str:
    clean = " ".join((text or "").split())

    if len(clean) <= max_len:
        return clean

    return clean[: max_len - 3].rstrip() + "..."


def _mode_to_hud_name(mode: int) -> str:
    return {
        MODE_AUDIO: "audio",
        MODE_FACE: "face",
        MODE_DUAL: "both",
    }.get(mode, "face")


def _get_cpu_temp() -> Optional[float]:
    if platform.system() != "Linux":
        return None

    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as temp_file:
            return float(temp_file.read().strip()) / 1000.0
    except Exception:
        return None


def _get_wifi_signal() -> str:
    if platform.system() != "Linux":
        return "N/A"

    try:
        output = subprocess.check_output(["iwconfig", "wlan0"], stderr=subprocess.DEVNULL).decode("utf-8")
    except Exception:
        return "N/A"

    marker = "Signal level="
    if marker not in output:
        return "N/A"

    signal = output.split(marker, 1)[1].split()[0].strip()
    return f"{signal} dBm"


def _face_box_to_display(face: Dict[str, Any], width: int, height: int) -> Optional[tuple[int, int, int, int]]:
    box = face.get("box", [])
    rect = face.get("rect")

    if len(box) == 4:
        left, top, right, bottom = box
    elif rect is not None and all(hasattr(rect, attr) for attr in ("left", "top", "right", "bottom")):
        left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    else:
        return None

    source_w = max(int(face.get("frame_width", FACE_SEND_WIDTH) or FACE_SEND_WIDTH), 1)
    source_h = max(int(face.get("frame_height", height) or height), 1)

    scale_x = width / float(source_w)
    scale_y = height / float(source_h)

    left = int(left * scale_x)
    right = int(right * scale_x)
    top = int(top * scale_y)
    bottom = int(bottom * scale_y)

    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width - 1, right))
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height - 1, bottom))

    return left, top, right, bottom


def render_clock_date(frame, x: int = 20, y: int = 42):
    now = datetime.now()
    time_str = now.strftime("%I:%M %p").lstrip("0")
    date_str = now.strftime("%a, %b %d")

    cv2.putText(frame, time_str, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(frame, date_str, (x, y + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1, cv2.LINE_AA)


def render_system_status(
    frame,
    mode: str,
    uart_available: bool,
    audio_available: bool,
    face_available: bool,
    width: int,
):
    x = width - 430
    y = 30

    temp = _get_cpu_temp()
    temp_color = (0, 255, 0)
    temp_text = "CPU --.-C"

    if temp is not None:
        temp_text = f"CPU {temp:.1f}C"
        if temp > 75:
            temp_color = (0, 0, 255)
        elif temp > 60:
            temp_color = (0, 255, 255)

    cv2.putText(frame, temp_text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, temp_color, 1, cv2.LINE_AA)
    cv2.putText(
        frame,
        f"WiFi {_get_wifi_signal()}",
        (x + 130, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )

    y += 25
    server_available = audio_available or face_available
    server_color = (0, 255, 0) if server_available else (0, 0, 255)
    server_text = "Connected" if server_available else "Disconnected"

    cv2.circle(frame, (x + 8, y - 4), 5, server_color, -1)
    cv2.putText(frame, f"DGX {server_text}", (x + 22, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

    detail = f"A:{'OK' if audio_available else '--'} F:{'OK' if face_available else '--'} UART:{'OK' if uart_available else '--'}"
    cv2.putText(frame, detail, (x + 155, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)

    y += 25
    cv2.putText(frame, f"Mode: {mode.upper()}", (x + 22, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 0), 2, cv2.LINE_AA)


def render_face_recognition(frame, faces_info: List[Dict[str, Any]], width: int, height: int):
    for face in faces_info:
        scaled_box = _face_box_to_display(face, width, height)

        if scaled_box is None:
            continue

        left, top, right, bottom = scaled_box
        info_x = right + 12

        if info_x > width - 280:
            info_x = max(12, left - 280)

        info_y = max(top + 18, 30)

        cv2.rectangle(frame, (left, top), (right, bottom), (0, 255, 0), 2)

        name = _trim_text(face.get("name", "Unknown"), max_len=28)
        seen_count = int(face.get("seen_count") or 0)
        label = name if seen_count <= 0 else f"{name} (seen {seen_count}x)"
        cv2.putText(frame, label, (info_x, info_y), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (255, 255, 255), 2, cv2.LINE_AA)

        last_seen = face.get("last_seen_ago")
        if last_seen:
            cv2.putText(frame, f"Last: {last_seen}", (info_x, info_y + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)

        summary = _trim_text(face.get("summary", ""), max_len=48)
        if summary:
            cv2.putText(frame, summary, (info_x, info_y + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

        if face.get("is_recording"):
            rec_x = max(left - 18, 12)
            cv2.circle(frame, (rec_x, top + 10), 6, (0, 0, 255), -1)
            cv2.putText(frame, "REC", (rec_x - 12, top + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1, cv2.LINE_AA)


def render_live_captions(frame, text: str, source_language: str, width: int, height: int):
    if not text:
        return

    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.8
    thickness = 2
    max_width = width - 40
    words = text.split()
    lines = []
    current_line = ""

    language_code = (source_language or "").strip().lower()
    show_language_tag = language_code and language_code not in {"en", "eng", "english"}

    if show_language_tag:
        lang_map = {
            "es": "Spanish",
            "de": "German",
            "fr": "French",
            "it": "Italian",
            "hi": "Hindi",
            "zh": "Chinese",
        }
        current_line = f"({lang_map.get(language_code, language_code.upper())}) "

    for word in words:
        test_line = current_line + word + " "
        (text_width, _), _ = cv2.getTextSize(test_line, font, scale, thickness)

        if text_width > max_width and current_line:
            lines.append(current_line.strip())
            current_line = word + " "
        else:
            current_line = test_line

    if current_line:
        lines.append(current_line.strip())

    lines_to_show = lines[-2:]
    bg_height = len(lines_to_show) * 35 + 20
    y_start = height - bg_height

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, y_start), (width, height), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

    y = y_start + 30

    for line in lines_to_show:
        if line.startswith("(") and show_language_tag:
            end_idx = line.find(")") + 1
            prefix = line[:end_idx]
            remainder = line[end_idx:]

            cv2.putText(frame, prefix, (20, y), font, scale, (0, 255, 255), thickness, cv2.LINE_AA)
            (prefix_width, _), _ = cv2.getTextSize(prefix, font, scale, thickness)
            cv2.putText(frame, remainder, (20 + prefix_width, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        else:
            cv2.putText(frame, line, (20, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

        y += 35


def render_reminders(frame, reminders: List[str], height: int):
    if not reminders:
        return

    y = height - 120

    for text in reminders[:3]:
        cv2.circle(frame, (25, y - 5), 4, (0, 200, 255), -1)
        cv2.putText(frame, _trim_text(text, max_len=46), (40, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        y -= 25


def render_toasts(frame, toast_manager: ToastManager, width: int, height: int):
    active_toasts = toast_manager.get_active_toasts()

    if not active_toasts:
        return

    y = height // 2

    for toast in active_toasts:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.8
        thickness = 2
        (text_width, _), _ = cv2.getTextSize(toast.text, font, scale, thickness)
        x = (width - text_width) // 2

        alpha = toast.alpha
        value = int(255 * alpha)

        if alpha > 0.05:
            cv2.putText(frame, toast.text, (x, y), font, scale, (value, value, value), thickness, cv2.LINE_AA)
            y += 40


class DisplayManager:
    def __init__(self, resolution=(1280, 720)):
        self.width, self.height = resolution
        self.window_name = HUD_WINDOW_NAME
        self.toast_manager = ToastManager()
        self.pending_action: Optional[str] = None
        self.is_running = False

    def start(self):
        if self.is_running:
            return

        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)

        try:
            cv2.setWindowProperty(self.window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        except Exception:
            pass

        self.is_running = True
        logger.info("HUD display started")

    def stop(self):
        if not self.is_running:
            return

        self.is_running = False

        try:
            cv2.destroyWindow(self.window_name)
        except Exception:
            cv2.destroyAllWindows()

        logger.info("HUD display stopped")

    def render_frame(
        self,
        mode: str,
        uart_available: bool,
        audio_available: bool,
        face_available: bool,
        faces_info: List[Dict[str, Any]],
        caption_text: str,
        source_language: str,
        reminders: List[str],
        bg_frame: Optional[np.ndarray] = None,
    ) -> tuple[bool, Optional[str]]:
        if not self.is_running:
            return False, None

        if bg_frame is not None:
            frame = cv2.resize(bg_frame, (self.width, self.height))
        else:
            frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        render_clock_date(frame)
        render_system_status(frame, mode, uart_available, audio_available, face_available, self.width)
        render_reminders(frame, reminders, self.height)
        render_toasts(frame, self.toast_manager, self.width, self.height)

        if mode in ("face", "both"):
            render_face_recognition(frame, faces_info, self.width, self.height)

        if mode in ("audio", "both"):
            render_live_captions(frame, caption_text, source_language, self.width, self.height)

        cv2.imshow(self.window_name, frame)

        key = cv2.waitKey(1) & 0xFF
        self.pending_action = None

        if key == ord("q"):
            return False, None

        if key == ord("a"):
            return True, "audio"

        if key == ord("f"):
            return True, "face"

        if key in (ord("b"), ord("d")):
            return True, "dual"

        if key == ord("m"):
            self.pending_action = "maps"

        elif key == ord("s"):
            self.pending_action = "summary"

        return True, None

    def consume_action(self) -> Optional[str]:
        action = self.pending_action
        self.pending_action = None
        return action

    def show_toast(self, text: str, duration: float = 2.0):
        self.toast_manager.show(text, duration)


display_manager = DisplayManager((DISPLAY_WIDTH, DISPLAY_HEIGHT))


def add_reminder(text: str):
    clean = _trim_text(text, max_len=56)

    if not clean:
        return

    with state_lock:
        reminders = [item for item in state["reminders"] if item != clean]
        reminders.insert(0, clean)
        state["reminders"] = reminders[:3]


def show_hud_toast(text: str, duration: float = 2.0):
    display_manager.show_toast(text, duration)


def set_mode_state(mode: int, toast: bool = True):
    if mode not in MODE_NAMES:
        return

    with state_lock:
        state["mode"] = mode

    if toast:
        show_hud_toast(f"{MODE_NAMES[mode].title()} mode", duration=1.5)


# =========================
# Camera Display
# =========================

def maybe_send_frame_to_dgx(frame, last_send_time):
    now = time.time()

    if now - last_send_time < FACE_SEND_INTERVAL_SECONDS:
        return last_send_time

    with state_lock:
        mode = state["mode"]

    if mode not in [MODE_FACE, MODE_DUAL]:
        return last_send_time

    h, w = frame.shape[:2]
    scale = FACE_SEND_WIDTH / float(w)
    new_h = int(h * scale)

    small = cv2.resize(frame, (FACE_SEND_WIDTH, new_h))

    ok, jpg = cv2.imencode(
        ".jpg",
        small,
        [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    )

    if not ok:
        return last_send_time

    jpeg_bytes = jpg.tobytes()

    try:
        if frame_queue.full():
            try:
                frame_queue.get_nowait()
            except Exception:
                pass

        frame_queue.put_nowait(jpeg_bytes)

    except Exception:
        pass

    return now


def camera_display_thread():
    print("[PI] Starting camera using old TrueVision camera backend")

    # Reuse the same camera logic that worked in your old code:
    # Picamera2 on Raspberry Pi, OpenCV fallback for USB webcam.
    try:
        from picamera2 import Picamera2
        PICAMERA2_AVAILABLE = True
    except Exception as e:
        print("[PI] Picamera2 not available:", e)
        Picamera2 = None
        PICAMERA2_AVAILABLE = False

    class PiCam2Capture:
        def __init__(self, width: int, height: int):
            if Picamera2 is None:
                raise RuntimeError("Picamera2 is not available")

            self._picam2 = Picamera2()

            config = self._picam2.create_preview_configuration(
                main={
                    "size": (width, height),
                    "format": "BGR888"
                }
            )

            self._picam2.configure(config)
            self._picam2.start()
            time.sleep(1.0)

        def read(self):
            arr = self._picam2.capture_array()

            if arr is None:
                return False, None

            if len(arr.shape) == 2:
                frame = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            elif arr.shape[2] == 4:
                frame = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
            else:
                frame = arr

            return True, frame

        def release(self):
            self._picam2.stop()
            try:
                self._picam2.close()
            except Exception:
                pass

    class OpenCVCapture:
        def __init__(self, width: int, height: int, fps: int = 30, camera_index: int = 0):
            self._cap = cv2.VideoCapture(camera_index, cv2.CAP_ANY)

            if not self._cap.isOpened():
                raise RuntimeError(f"Could not open webcam index {camera_index}")

            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self._cap.set(cv2.CAP_PROP_FPS, fps)

        def read(self):
            ok, frame = self._cap.read()

            if not ok or frame is None:
                return False, None

            if len(frame.shape) == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            return True, frame

        def release(self):
            self._cap.release()

    try:
        if PICAMERA2_AVAILABLE:
            print("[PI] Camera: Using Picamera2")
            cap = PiCam2Capture(DISPLAY_WIDTH, DISPLAY_HEIGHT)
        else:
            print("[PI] Camera: Using OpenCV fallback")
            cap = OpenCVCapture(DISPLAY_WIDTH, DISPLAY_HEIGHT, fps=30, camera_index=CAMERA_INDEX)

    except Exception as e:
        print("[PI] Could not start camera:", e)
        return

    display_manager.start()
    last_face_send = 0

    while state["running"]:
        ret, camera_frame = cap.read()

        if not ret or camera_frame is None:
            print("[PI] Camera frame failed")
            time.sleep(0.05)
            continue

        camera_frame = cv2.resize(camera_frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))

        with state_lock:
            mode = state["mode"]
            caption = state["caption"]
            language = state["last_language"]
            faces = list(state["faces"])
            reminders = list(state["reminders"])
            uart_connected = state["uart_connected"]
            audio_connected = state["dgx_audio_connected"]
            face_connected = state["dgx_face_connected"]

        last_face_send = maybe_send_frame_to_dgx(camera_frame, last_face_send)

        keep_running, new_mode = display_manager.render_frame(
            mode=_mode_to_hud_name(mode),
            uart_available=uart_connected,
            audio_available=audio_connected,
            face_available=face_connected,
            faces_info=faces,
            caption_text=caption,
            source_language=language,
            reminders=reminders,
            bg_frame=camera_frame if HUD_SHOW_CAMERA_BG else None,
        )

        if not keep_running:
            with state_lock:
                state["running"] = False
            break

        if new_mode in MODE_FROM_NAME:
            set_mode_state(MODE_FROM_NAME[new_mode])

        action = display_manager.consume_action()

        if action == "maps":
            threading.Thread(target=open_maps, daemon=True).start()

        elif action == "summary":
            threading.Thread(target=fetch_summary, daemon=True).start()

    cap.release()
    display_manager.stop()
    cv2.destroyAllWindows()


# =========================
# Extra Features
# =========================

def open_maps():
    with state_lock:
        loc = state["location"]

    if loc and "lat" in loc and "lon" in loc:
        url = f"https://www.google.com/maps?q={loc['lat']},{loc['lon']}"
    else:
        url = "https://www.google.com/maps"

    print("[PI] Opening maps:", url)

    try:
        subprocess.Popen(["chromium-browser", "--new-window", url])
    except Exception:
        webbrowser.open(url)


def open_music():
    url = "https://music.youtube.com"

    print("[PI] Opening music")

    try:
        subprocess.Popen(["chromium-browser", "--new-window", url])
    except Exception:
        webbrowser.open(url)


def open_video_call():
    url = "https://meet.google.com"

    print("[PI] Opening video call page")

    try:
        subprocess.Popen(["chromium-browser", "--new-window", url])
    except Exception:
        webbrowser.open(url)


def fetch_weather():
    if not WEATHER_API_KEY:
        msg = "Weather API key not set on Pi."
        with state_lock:
            state["weather"] = msg
            state["caption"] = msg
        add_reminder("Weather unavailable")
        show_hud_toast("Weather unavailable", duration=1.5)
        return

    with state_lock:
        loc = state["location"]

    if loc and "lat" in loc and "lon" in loc:
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?lat={loc['lat']}&lon={loc['lon']}&appid={WEATHER_API_KEY}&units=imperial"
        )
    else:
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?q=West Lafayette,US&appid={WEATHER_API_KEY}&units=imperial"
        )

    try:
        data = requests.get(url, timeout=10).json()

        desc = data["weather"][0]["description"]
        temp = data["main"]["temp"]

        msg = f"Weather: {temp:.0f}°F, {desc}"

        with state_lock:
            state["weather"] = msg
            state["caption"] = msg

        add_reminder(msg)
        show_hud_toast("Weather updated", duration=1.5)

        print("[PI]", msg)

    except Exception as e:
        msg = f"Weather failed: {e}"

        with state_lock:
            state["caption"] = msg

        add_reminder("Weather fetch failed")
        show_hud_toast("Weather failed", duration=1.5)

        print("[PI]", msg)


def fetch_news():
    if not NEWS_API_KEY:
        msg = "News API key not set on Pi."

        with state_lock:
            state["news"] = msg
            state["caption"] = msg

        add_reminder("News unavailable")
        show_hud_toast("News unavailable", duration=1.5)

        return

    url = (
        "https://newsapi.org/v2/top-headlines"
        f"?country=us&pageSize=3&apiKey={NEWS_API_KEY}"
    )

    try:
        data = requests.get(url, timeout=10).json()
        articles = data.get("articles", [])

        headlines = [a.get("title", "") for a in articles[:3]]
        msg = "News: " + " | ".join(headlines)

        with state_lock:
            state["news"] = msg
            state["caption"] = msg

        add_reminder("Headlines updated")
        show_hud_toast("News updated", duration=1.5)

        print("[PI]", msg)

    except Exception as e:
        msg = f"News failed: {e}"

        with state_lock:
            state["caption"] = msg

        add_reminder("News fetch failed")
        show_hud_toast("News failed", duration=1.5)

        print("[PI]", msg)


def fetch_summary():
    try:
        resp = requests.get(f"{DGX_HTTP_URL}/conversation_summary", timeout=60)
        data = resp.json()

        summary = data.get("summary", "")

        with state_lock:
            state["summary"] = summary
            state["caption"] = "Summary: " + summary

        add_reminder("Conversation summary ready")
        show_hud_toast("Summary ready", duration=1.5)

        print("[SUMMARY]", summary)

    except Exception as e:
        msg = f"Summary failed: {e}"

        with state_lock:
            state["caption"] = msg

        add_reminder("Summary failed")
        show_hud_toast("Summary failed", duration=1.5)

        print("[PI]", msg)


def rename_face_on_dgx(old_name: str, new_name: str):
    try:
        resp = requests.post(
            f"{DGX_HTTP_URL}/rename_face",
            json={
                "old_name": old_name,
                "new_name": new_name
            },
            timeout=10
        )

        return resp.json()

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


# =========================
# Phone Controller
# =========================

phone_app = FastAPI()


CONTROL_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TrueVision Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        body {
            font-family: Arial, sans-serif;
            background: #0f172a;
            color: white;
            text-align: center;
            padding: 18px;
        }

        h1 {
            margin-bottom: 4px;
        }

        .small {
            color: #cbd5e1;
            font-size: 14px;
            margin-bottom: 20px;
        }

        button {
            width: 94%;
            padding: 17px;
            margin: 7px;
            font-size: 19px;
            border-radius: 14px;
            border: none;
            font-weight: bold;
        }

        input {
            width: 88%;
            padding: 14px;
            margin: 6px;
            border-radius: 10px;
            border: none;
            font-size: 16px;
        }

        .audio { background: #2563eb; color: white; }
        .face { background: #16a34a; color: white; }
        .dual { background: #9333ea; color: white; }
        .feature { background: #f59e0b; color: black; }
        .danger { background: #dc2626; color: white; }
        .gray { background: #475569; color: white; }

        pre {
            text-align: left;
            background: #020617;
            padding: 14px;
            border-radius: 12px;
            overflow-x: auto;
            white-space: pre-wrap;
        }
    </style>
</head>

<body>
    <h1>TrueVision</h1>
    <div class="small">Phone Controller</div>

    <button class="audio" onclick="setMode('audio')">Audio Mode</button>
    <button class="face" onclick="setMode('face')">Face Mode</button>
    <button class="dual" onclick="setMode('dual')">Dual Mode</button>

    <hr>

    <button class="feature" onclick="feature('maps')">Open Maps</button>
    <button class="feature" onclick="feature('weather')">Weather</button>
    <button class="feature" onclick="feature('news')">News</button>
    <button class="feature" onclick="feature('summary')">Summarize Conversation</button>
    <button class="feature" onclick="feature('music')">Music</button>
    <button class="feature" onclick="feature('call')">Video Call</button>

    <hr>

    <button class="gray" onclick="sendLocation()">Send Phone Location to Pi</button>

    <hr>

    <h3>Rename Face</h3>
    <input id="oldName" placeholder="Old name, example Person_001">
    <input id="newName" placeholder="New name, example Professor">
    <button class="gray" onclick="renameFace()">Rename</button>

    <hr>

    <button class="gray" onclick="refreshState()">Refresh State</button>

    <pre id="status">Ready</pre>

<script>
async function setMode(mode) {
    const res = await fetch('/mode/' + mode, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function feature(name) {
    const res = await fetch('/feature/' + name, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

function sendLocation() {
    if (!navigator.geolocation) {
        document.getElementById('status').innerText = 'Geolocation not supported';
        return;
    }

    navigator.geolocation.getCurrentPosition(async function(pos) {
        const payload = {
            lat: pos.coords.latitude,
            lon: pos.coords.longitude
        };

        const res = await fetch('/location', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        document.getElementById('status').innerText = JSON.stringify(data, null, 2);
    }, function(err) {
        document.getElementById('status').innerText = 'Location error: ' + err.message;
    });
}

async function renameFace() {
    const oldName = document.getElementById('oldName').value.trim();
    const newName = document.getElementById('newName').value.trim();

    if (!oldName || !newName) {
        document.getElementById('status').innerText = 'Please enter old name and new name';
        return;
    }

    const res = await fetch('/rename_face', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            old_name: oldName,
            new_name: newName
        })
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function refreshState() {
    const res = await fetch('/state');
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

setInterval(refreshState, 4000);
</script>
</body>
</html>
"""


@phone_app.get("/", response_class=HTMLResponse)
def controller_home():
    return CONTROL_HTML


@phone_app.post("/mode/{mode_name}")
def set_mode(mode_name: str):
    if mode_name not in MODE_FROM_NAME:
        return {
            "ok": False,
            "error": "Unknown mode"
        }

    set_mode_state(MODE_FROM_NAME[mode_name])

    return {
        "ok": True,
        "mode": mode_name
    }


@phone_app.post("/feature/{feature_name}")
def run_feature(feature_name: str):
    if feature_name == "maps":
        threading.Thread(target=open_maps, daemon=True).start()
        show_hud_toast("Opening maps", duration=1.5)

    elif feature_name == "weather":
        threading.Thread(target=fetch_weather, daemon=True).start()
        show_hud_toast("Checking weather", duration=1.5)

    elif feature_name == "news":
        threading.Thread(target=fetch_news, daemon=True).start()
        show_hud_toast("Fetching headlines", duration=1.5)

    elif feature_name == "summary":
        threading.Thread(target=fetch_summary, daemon=True).start()
        show_hud_toast("Building summary", duration=1.5)

    elif feature_name == "music":
        threading.Thread(target=open_music, daemon=True).start()
        show_hud_toast("Opening music", duration=1.5)

    elif feature_name == "call":
        threading.Thread(target=open_video_call, daemon=True).start()
        show_hud_toast("Opening video call", duration=1.5)

    else:
        return {
            "ok": False,
            "error": "Unknown feature"
        }

    return {
        "ok": True,
        "feature": feature_name
    }


@phone_app.post("/location")
def update_location(payload: Dict[str, float]):
    with state_lock:
        state["location"] = {
            "lat": payload["lat"],
            "lon": payload["lon"]
        }

    add_reminder("Phone location synced")
    show_hud_toast("Location updated", duration=1.5)

    return {
        "ok": True,
        "location": state["location"]
    }


@phone_app.post("/rename_face")
def rename_face(payload: Dict[str, str]):
    old_name = payload.get("old_name", "").strip()
    new_name = payload.get("new_name", "").strip()

    if not old_name or not new_name:
        return {
            "ok": False,
            "error": "Missing old_name or new_name"
        }

    result = rename_face_on_dgx(old_name, new_name)

    if result.get("ok"):
        add_reminder(f"Renamed {old_name} to {new_name}")
        show_hud_toast(f"Renamed {old_name}", duration=1.5)
    else:
        show_hud_toast("Rename failed", duration=1.5)

    return result


@phone_app.get("/state")
def get_state():
    with state_lock:
        return {
            "running": state["running"],
            "mode": MODE_NAMES.get(state["mode"], "UNKNOWN"),
            "caption": state["caption"],
            "last_original_text": state["last_original_text"],
            "last_language": state["last_language"],
            "summary": state["summary"],
            "faces": state["faces"],
            "weather": state["weather"],
            "news": state["news"],
            "location": state["location"],
            "reminders": state["reminders"],
            "uart_connected": state["uart_connected"],
            "dgx_audio_connected": state["dgx_audio_connected"],
            "dgx_face_connected": state["dgx_face_connected"],
            "last_face_error": state["last_face_error"],
        }


def phone_controller_thread():
    print("[PI] Phone controller running on port 8080")
    uvicorn.run(
        phone_app,
        host="0.0.0.0",
        port=8080,
        log_level="warning"
    )


# =========================
# Main
# =========================

def main():
    print("[PI] Starting TrueVision Pi App")
    print("[PI] DGX HTTP:", DGX_HTTP_URL)
    print("[PI] DGX Audio WS:", DGX_AUDIO_WS_URL)
    print("[PI] DGX Face WS:", DGX_FACE_WS_URL)

    threads = [
        threading.Thread(target=uart_thread, daemon=True),
        threading.Thread(target=dgx_audio_thread, daemon=True),
        threading.Thread(target=dgx_face_thread, daemon=True),
        threading.Thread(target=phone_controller_thread, daemon=True),
    ]

    for t in threads:
        t.start()

    camera_display_thread()

    with state_lock:
        state["running"] = False

    print("[PI] TrueVision stopped")


if __name__ == "__main__":
    main()
