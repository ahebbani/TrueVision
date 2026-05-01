import os
import cv2
import json
import time
import wave
import queue
import serial
import threading
import subprocess
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any, List

import numpy as np
import requests
import websocket
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn


# =========================
# Config
# =========================

DGX_HTTP_URL = "http://10.186.78.209:8008"
DGX_AUDIO_WS_URL = "ws://10.186.78.209:8008/ws/audio"
DGX_FACE_WS_URL = "ws://10.186.78.209:8008/ws/face"

SERIAL_PORT = "/dev/serial0"
SERIAL_BAUD = 921600

CAMERA_INDEX = 0

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2

AUDIO_CHUNK_SECONDS = 2.5
AUDIO_CHUNK_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH_BYTES * AUDIO_CHUNK_SECONDS)

FACE_SEND_INTERVAL_SECONDS = 0.35
FACE_SEND_WIDTH = 320
JPEG_QUALITY = 70

DISPLAY_WIDTH = 640
DISPLAY_HEIGHT = 480

# Default location: West Lafayette, Indiana
DEFAULT_LAT = 40.4237
DEFAULT_LON = -86.9212

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
    MODE_DUAL: "DUAL",
}

MODE_FROM_NAME = {
    "audio": MODE_AUDIO,
    "face": MODE_FACE,
    "dual": MODE_DUAL,
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
    "selected_language": "en",
    "last_task": "",
    "last_command": {},
    "summary": "",

    "faces": [],
    "last_face_error": "",

    "weather": "",
    "weather_temp": "--",
    "weather_last_updated": 0,

    "news": "",

    "location": {
        "lat": DEFAULT_LAT,
        "lon": DEFAULT_LON,
    },
    "location_label": "West Lafayette, IN",

    "hud_camera_background": True,

    "reminders": [],
    "telegram_notifications": [],

    "youtube_status": "",
    "youtube_last_query": "",

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
            timeout=0.05,
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
            "payload": payload,
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
                with state_lock:
                    state["mode"] = mode

                print(f"[PI] ESP32 mode: {MODE_NAMES[mode]}")

        elif pkt_type == PKT_AUDIO:
            with state_lock:
                mode = state["mode"]

            if mode in [MODE_AUDIO, MODE_DUAL]:
                try:
                    audio_queue.put_nowait(payload)
                except queue.Full:
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
                    selected_language = data.get("selected_language", "")
                    task = data.get("task", "")

                    if text:
                        with state_lock:
                            state["caption"] = text
                            state["last_original_text"] = original
                            state["last_language"] = language
                            state["selected_language"] = selected_language
                            state["last_task"] = task
                            state["last_command"] = data.get("command", {})

                        print("[CAPTION]", text)
                        print("[LANG]", language, "| selected:", selected_language, "|", task)

        except Exception as e:
            print("[PI] Audio WebSocket failed:", e)

            with state_lock:
                state["dgx_audio_connected"] = False

            time.sleep(2)


# =========================
# Face Frames to DGX
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
# Telegram Notifications
# =========================

def telegram_notifications_thread():
    while state["running"]:
        try:
            resp = requests.get(
                f"{DGX_HTTP_URL}/telegram_notifications",
                timeout=5,
            )

            data = resp.json()
            messages = data.get("messages", [])

            with state_lock:
                state["telegram_notifications"] = messages[-5:]

        except Exception:
            pass

        time.sleep(2)


# =========================
# HUD Drawing Helpers
# =========================

def sanitize_hud_text(text: str) -> str:
    """
    OpenCV Hershey fonts cannot render many Unicode symbols.
    This removes/replaces characters that show up as ??? on the HUD.
    """
    if text is None:
        return ""

    text = str(text)
    replacements = {
        "°": "",
        "•": "-",
        "—": "-",
        "–": "-",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text.encode("ascii", errors="ignore").decode("ascii")


def wrap_text(text: str, max_chars: int = 64) -> List[str]:
    text = sanitize_hud_text(text)
    words = text.split()
    lines = []
    current = ""

    for word in words:
        if len(current) + len(word) + 1 <= max_chars:
            current += (" " if current else "") + word
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def format_timestamp(ts):
    if not ts:
        return "never"

    try:
        return datetime.fromtimestamp(float(ts)).strftime("%b %d %I:%M %p").replace(" 0", " ")
    except Exception:
        return "unknown"


def format_time_ago(ts):
    if not ts:
        return "never"

    try:
        diff = time.time() - float(ts)

        if diff < 60:
            return "just now"

        if diff < 3600:
            return f"{int(diff // 60)}m ago"

        if diff < 86400:
            return f"{int(diff // 3600)}h ago"

        return f"{int(diff // 86400)}d ago"

    except Exception:
        return "unknown"


def get_cpu_temp_c() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return float(f.read().strip()) / 1000.0
    except Exception:
        return 0.0


def get_wifi_signal() -> str:
    try:
        output = subprocess.check_output(
            ["iwconfig", "wlan0"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore")

        if "Signal level=" in output:
            idx = output.find("Signal level=") + len("Signal level=")
            val = output[idx:idx + 7].split()[0]
            return f"{val} dBm"

    except Exception:
        pass

    return "N/A"


def hud_text(frame, text, pos, scale, color, thickness=1):
    clean = sanitize_hud_text(text)
    cv2.putText(
        frame,
        clean,
        pos,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def hud_draw_clock_date(frame):
    now = datetime.now()

    time_str = now.strftime("%I:%M %p").lstrip("0")
    date_str = now.strftime("%a, %b %d")

    hud_text(frame, time_str, (12, 30), 0.78, (255, 255, 255), 2)
    hud_text(frame, date_str, (13, 52), 0.43, (205, 205, 205), 1)


def hud_draw_weather_slot(frame):
    with state_lock:
        temp = state.get("weather_temp", "--")
        last_updated = state.get("weather_last_updated", 0)
        location_label = state.get("location_label", "West Lafayette, IN")

    if not temp:
        temp = "--"

    x = 14
    y = 76

    hud_text(frame, "TEMP", (x, y), 0.34, (0, 220, 255), 1)
    hud_text(frame, temp, (x + 52, y + 1), 0.46, (255, 255, 255), 1)
    hud_text(frame, location_label[:20], (x, y + 17), 0.30, (210, 210, 210), 1)

    if last_updated:
        age = format_time_ago(last_updated)
        hud_text(frame, age, (x + 112, y + 1), 0.30, (170, 170, 170), 1)


def hud_draw_system_status(frame):
    h, w = frame.shape[:2]

    with state_lock:
        mode = state["mode"]
        uart_connected = state["uart_connected"]
        audio_connected = state["dgx_audio_connected"]
        face_connected = state["dgx_face_connected"]
        selected_language = state["selected_language"]
        last_language = state["last_language"]
        hud_camera_background = state["hud_camera_background"]
        youtube_status = state["youtube_status"]

    mode_name = MODE_NAMES.get(mode, "UNKNOWN")
    server_available = audio_connected and face_connected

    x = w - 230
    y = 22

    temp = get_cpu_temp_c()
    temp_color = (0, 255, 0)

    if temp > 75:
        temp_color = (0, 0, 255)
    elif temp > 60:
        temp_color = (0, 255, 255)

    hud_text(frame, f"CPU {temp:.1f}C", (x, y), 0.40, temp_color, 1)

    wifi = get_wifi_signal()
    hud_text(frame, f"WiFi {wifi}", (x + 90, y), 0.40, (255, 255, 255), 1)

    y += 18

    server_color = (0, 255, 0) if server_available else (0, 0, 255)
    server_text = "OK" if server_available else "NO"

    cv2.circle(frame, (x + 6, y - 4), 4, server_color, -1)
    hud_text(frame, f"DGX: {server_text}", (x + 16, y), 0.40, (255, 255, 255), 1)

    y += 18

    bg_text = "Cam" if hud_camera_background else "Black"
    hud_text(frame, f"{mode_name} | HUD:{bg_text}", (x + 16, y), 0.40, (255, 210, 0), 1)

    y += 18

    hud_text(frame, f"UART: {'OK' if uart_connected else 'NO'}", (x + 16, y), 0.36, (180, 220, 255), 1)

    y += 17

    lang_text = f"Lang:{selected_language}"

    if last_language:
        lang_text += f" Det:{last_language}"

    hud_text(frame, lang_text[:30], (x + 16, y), 0.34, (180, 220, 255), 1)

    if youtube_status:
        y += 17
        hud_text(frame, f"YT: {youtube_status[:22]}", (x + 16, y), 0.34, (255, 180, 180), 1)


def hud_draw_info_cards(frame):
    with state_lock:
        news = state["news"]
        summary = state["summary"]
        telegram_notifications = list(state["telegram_notifications"])
        youtube_last_query = state["youtube_last_query"]

    y = 112

    cards = []

    for msg in telegram_notifications[-3:][::-1]:
        sender = msg.get("sender", "Telegram")
        text = msg.get("text", "")
        msg_time = format_time_ago(msg.get("date"))

        cards.append((
            "TELEGRAM",
            f"{sender}: {text[:55]} ({msg_time})",
        ))

    if youtube_last_query:
        cards.append(("YOUTUBE", f"Last search: {youtube_last_query[:55]}"))

    if news:
        cards.append(("NEWS", news[:70]))

    if summary:
        cards.append(("SUMMARY", summary[:70]))

    for title, text in cards[:4]:
        hud_text(frame, title, (14, y), 0.36, (0, 220, 255), 1)
        y += 17

        lines = wrap_text(text, max_chars=32)

        for line in lines[:2]:
            hud_text(frame, line, (14, y), 0.38, (230, 230, 230), 1)
            y += 17

        y += 9


def hud_draw_reminders_slot(frame):
    h, w = frame.shape[:2]

    with state_lock:
        reminders = list(state.get("reminders", []))

    box_x = w - 230
    box_y = 155
    box_w = 215
    box_h = 110

    overlay = frame.copy()

    cv2.rectangle(
        overlay,
        (box_x, box_y),
        (box_x + box_w, box_y + box_h),
        (15, 15, 15),
        -1,
    )

    cv2.addWeighted(overlay, 0.58, frame, 0.42, 0, frame)

    cv2.rectangle(
        frame,
        (box_x, box_y),
        (box_x + box_w, box_y + box_h),
        (70, 70, 70),
        1,
    )

    hud_text(frame, "REMINDERS", (box_x + 9, box_y + 18), 0.36, (0, 220, 255), 1)

    if not reminders:
        hud_text(frame, "No reminders", (box_x + 9, box_y + 45), 0.38, (180, 180, 180), 1)
        return

    y = box_y + 42

    for reminder in reminders[-3:]:
        lines = wrap_text(reminder, max_chars=24)

        for line in lines[:2]:
            hud_text(frame, "- " + line[:26], (box_x + 9, y), 0.34, (235, 235, 235), 1)
            y += 16

            if y > box_y + box_h - 8:
                return


def hud_draw_faces(frame):
    h, w = frame.shape[:2]

    with state_lock:
        faces = list(state["faces"])

    for face in faces:
        box = face.get("box", [])
        source_w = face.get("frame_width", FACE_SEND_WIDTH)
        source_h = face.get("frame_height", int(FACE_SEND_WIDTH * h / max(w, 1)))

        if len(box) != 4:
            continue

        left, top, right, bottom = box

        scale_x = w / max(source_w, 1)
        scale_y = h / max(source_h, 1)

        left = int(left * scale_x)
        right = int(right * scale_x)
        top = int(top * scale_y)
        bottom = int(bottom * scale_y)

        name = face.get("name", "Unknown")
        known = face.get("known", True)
        count = face.get("seen_count", None)

        if known:
            last_seen = face.get("last_seen")
            last_seen_text = format_timestamp(last_seen)
            last_seen_ago = format_time_ago(last_seen)

            label = f"{name}"
            sub_label = f"seen {count}x | last {last_seen_ago}"
            sub_label_2 = last_seen_text
        else:
            label = "Unknown"
            sub_label = "save from phone"
            sub_label_2 = ""

        cv2.rectangle(frame, (left, top), (right, bottom), (0, 255, 0), 1)

        label_x = min(right + 8, w - 160)
        label_y = max(top + 18, 70)

        hud_text(frame, label[:18], (label_x, label_y), 0.48, (255, 255, 255), 1)
        hud_text(frame, sub_label[:30], (label_x, label_y + 18), 0.34, (180, 255, 180), 1)

        if sub_label_2:
            hud_text(frame, sub_label_2[:24], (label_x, label_y + 34), 0.32, (200, 200, 200), 1)


def hud_draw_captions(frame):
    with state_lock:
        caption = state["caption"]
        selected_language = state["selected_language"]

    if not caption:
        return

    h, w = frame.shape[:2]

    lang_map = {
        "en": "English",
        "es": "Spanish",
        "de": "German",
        "ar": "Arabic",
        "hi": "Hindi",
        "ur": "Urdu",
    }

    prefix = ""

    if selected_language and selected_language != "en":
        prefix = f"({lang_map.get(selected_language, selected_language.upper())}) "

    full_text = sanitize_hud_text(prefix + caption)

    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.50
    thickness = 1

    words = full_text.split()
    lines = []
    current = ""

    max_width = w - 50

    for word in words:
        test_line = current + word + " "

        text_size, _ = cv2.getTextSize(test_line, font, scale, thickness)

        if text_size[0] > max_width and current:
            lines.append(current.strip())
            current = word + " "
        else:
            current = test_line

    if current:
        lines.append(current.strip())

    lines_to_show = lines[-3:]

    bg_height = len(lines_to_show) * 24 + 20
    y_start = h - bg_height - 8

    overlay = frame.copy()
    cv2.rectangle(overlay, (8, y_start), (w - 8, h - 8), (25, 25, 25), -1)
    cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)

    y = y_start + 24

    for line in lines_to_show:
        hud_text(frame, line, (18, y), scale, (255, 255, 255), thickness)
        y += 24


def render_hud_frame(camera_frame):
    with state_lock:
        mode = state["mode"]
        use_camera_background = state["hud_camera_background"]

    if use_camera_background and camera_frame is not None:
        frame = cv2.resize(camera_frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
    else:
        frame = np.zeros((DISPLAY_HEIGHT, DISPLAY_WIDTH, 3), dtype=np.uint8)

    hud_draw_clock_date(frame)
    hud_draw_weather_slot(frame)
    hud_draw_system_status(frame)
    hud_draw_info_cards(frame)
    hud_draw_reminders_slot(frame)

    if mode in [MODE_FACE, MODE_DUAL]:
        hud_draw_faces(frame)

    if mode in [MODE_AUDIO, MODE_DUAL]:
        hud_draw_captions(frame)

    return frame


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
        [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY],
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
    print("[PI] Starting camera using TrueVision camera backend")

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
                    "format": "BGR888",
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

    last_face_send = 0

    cv2.namedWindow("TrueVision", cv2.WINDOW_NORMAL)
    cv2.moveWindow("TrueVision", 0, 0)

    try:
        cv2.setWindowProperty(
            "TrueVision",
            cv2.WND_PROP_FULLSCREEN,
            cv2.WINDOW_FULLSCREEN,
        )
    except Exception as e:
        print("[PI] Could not enable fullscreen:", e)

    while state["running"]:
        ret, frame = cap.read()

        if not ret or frame is None:
            print("[PI] Camera frame failed")
            time.sleep(0.05)
            continue

        camera_frame = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))

        last_face_send = maybe_send_frame_to_dgx(camera_frame, last_face_send)

        hud_frame = render_hud_frame(camera_frame)

        cv2.imshow("TrueVision", hud_frame)

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            state["running"] = False
            break

        elif key == ord("a"):
            with state_lock:
                state["mode"] = MODE_AUDIO

        elif key == ord("f"):
            with state_lock:
                state["mode"] = MODE_FACE

        elif key == ord("d") or key == ord("b"):
            with state_lock:
                state["mode"] = MODE_DUAL

        elif key == ord("m"):
            threading.Thread(target=open_maps, daemon=True).start()

        elif key == ord("s"):
            threading.Thread(target=fetch_summary, daemon=True).start()

        elif key == ord("h"):
            with state_lock:
                state["hud_camera_background"] = not state["hud_camera_background"]

    cap.release()
    cv2.destroyAllWindows()


# =========================
# Shell / Window Helpers
# =========================

def desktop_env():
    env = os.environ.copy()

    env.setdefault("DISPLAY", ":0")

    home = os.path.expanduser("~")
    xauth = os.path.join(home, ".Xauthority")

    if os.path.exists(xauth):
        env.setdefault("XAUTHORITY", xauth)

    env["PATH"] = env.get(
        "PATH",
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    )

    return env


def run_shell(cmd: List[str]):
    try:
        subprocess.Popen(
            cmd,
            env=desktop_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return {
            "ok": True,
            "cmd": cmd,
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "cmd": cmd,
        }


def run_shell_wait(cmd: List[str], timeout: int = 5):
    try:
        result = subprocess.run(
            cmd,
            env=desktop_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return {
            "ok": result.returncode == 0,
            "cmd": cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "cmd": cmd,
        }


def get_browser_cmd():
    candidates = [
        "firefox",
        "firefox-esr",
        "x-www-browser",
        "chromium-browser",
        "chromium",
        "google-chrome",
    ]

    for candidate in candidates:
        result = run_shell_wait(["which", candidate], timeout=2)

        if result.get("ok") and result.get("stdout", "").strip():
            return candidate

    return None


def focus_window_by_class(window_class: str):
    result = run_shell_wait(
        ["xdotool", "search", "--class", window_class],
        timeout=3,
    )

    if not result.get("ok") or not result.get("stdout", "").strip():
        return {
            "ok": False,
            "error": f"No window found for class {window_class}",
            "details": result,
        }

    window_ids = result["stdout"].strip().splitlines()
    target = window_ids[-1]

    return run_shell([
        "xdotool",
        "windowactivate",
        "--sync",
        target,
    ])


def focus_browser():
    classes = [
        "firefox",
        "Firefox",
        "Navigator",
        "chromium",
        "Chromium",
        "chrome",
        "Google-chrome",
    ]

    last_result = None

    for cls in classes:
        result = focus_window_by_class(cls)
        last_result = result

        if result.get("ok"):
            return result

    return {
        "ok": False,
        "error": "Could not focus browser",
        "last_result": last_result,
    }


def focus_truevision():
    result = run_shell_wait(
        ["xdotool", "search", "--name", "TrueVision"],
        timeout=3,
    )

    if not result.get("ok") or not result.get("stdout", "").strip():
        return {
            "ok": False,
            "error": "TrueVision HUD window not found",
            "details": result,
        }

    window_ids = result["stdout"].strip().splitlines()
    target = window_ids[-1]

    map_result = run_shell([
        "xdotool",
        "windowmap",
        target,
    ])

    time.sleep(0.15)

    raise_result = run_shell([
        "xdotool",
        "windowraise",
        target,
    ])

    time.sleep(0.15)

    activate_result = run_shell([
        "xdotool",
        "windowactivate",
        "--sync",
        target,
    ])

    time.sleep(0.15)

    return {
        "ok": True,
        "target": target,
        "map": map_result,
        "raise": raise_result,
        "activate": activate_result,
    }


def minimize_truevision():
    result = run_shell_wait(
        ["xdotool", "search", "--name", "TrueVision"],
        timeout=3,
    )

    if result.get("ok") and result.get("stdout", "").strip():
        window_ids = result["stdout"].strip().splitlines()
        target = window_ids[-1]

        return run_shell([
            "xdotool",
            "windowminimize",
            target,
        ])

    return {
        "ok": False,
        "error": "TrueVision window not found",
        "details": result,
    }


# =========================
# Mouse / Circular Joystick Control
# =========================

def mouse_move(dx: int, dy: int):
    dx = max(-80, min(80, int(dx)))
    dy = max(-80, min(80, int(dy)))

    return run_shell([
        "xdotool",
        "mousemove_relative",
        "--",
        str(dx),
        str(dy),
    ])


def mouse_click(button: int = 1):
    button = int(button)

    if button not in [1, 2, 3]:
        button = 1

    return run_shell([
        "xdotool",
        "click",
        str(button),
    ])


def mouse_scroll(direction: str):
    if direction == "up":
        return run_shell(["xdotool", "click", "4"])

    if direction == "down":
        return run_shell(["xdotool", "click", "5"])

    return {
        "ok": False,
        "error": "Unknown scroll direction",
    }


def mouse_drag_start():
    return run_shell(["xdotool", "mousedown", "1"])


def mouse_drag_end():
    return run_shell(["xdotool", "mouseup", "1"])


# =========================
# Keyboard / Phone Typing
# =========================

def keyboard_type_text(text: str):
    clean = str(text)

    if not clean:
        return {
            "ok": False,
            "error": "Missing text",
        }

    return run_shell([
        "xdotool",
        "type",
        "--clearmodifiers",
        "--delay",
        "5",
        "--",
        clean,
    ])


def keyboard_key(key_name: str):
    allowed = {
        "enter": "Return",
        "backspace": "BackSpace",
        "space": "space",
        "escape": "Escape",
        "tab": "Tab",
        "ctrl_l": "ctrl+l",
        "ctrl_a": "ctrl+a",
        "ctrl_c": "ctrl+c",
        "ctrl_v": "ctrl+v",
    }

    if key_name not in allowed:
        return {
            "ok": False,
            "error": f"Unknown key: {key_name}",
        }

    return run_shell([
        "xdotool",
        "key",
        "--clearmodifiers",
        allowed[key_name],
    ])


# =========================
# Browser / YouTube / Google
# =========================

def open_browser_url(url: str):
    browser = get_browser_cmd()

    if not browser:
        with state_lock:
            state["caption"] = "Could not find browser"

        return {
            "ok": False,
            "error": "Could not find Firefox/Chromium browser",
        }

    with state_lock:
        state["caption"] = f"Opening browser: {browser}"

    if "firefox" in browser:
        cmd = [
            browser,
            "--new-window",
            url,
        ]
    elif browser in ["x-www-browser", "sensible-browser"]:
        cmd = [
            browser,
            url,
        ]
    else:
        cmd = [
            browser,
            "--new-window",
            "--start-maximized",
            url,
        ]

    launch_result = run_shell(cmd)

    time.sleep(2.5)

    focus_result = focus_browser()
    minimize_result = minimize_truevision()

    time.sleep(0.4)
    focus_result_2 = focus_browser()

    return {
        "ok": True,
        "url": url,
        "browser": browser,
        "cmd": cmd,
        "launch": launch_result,
        "focus": focus_result,
        "minimize_hud": minimize_result,
        "focus_after_minimize": focus_result_2,
    }


def open_youtube_search(query: str):
    clean = query.strip()

    if not clean:
        return {
            "ok": False,
            "error": "Missing YouTube search query",
        }

    encoded = urllib.parse.quote_plus(clean)
    url = f"https://www.youtube.com/results?search_query={encoded}"

    with state_lock:
        state["youtube_status"] = "YouTube search"
        state["youtube_last_query"] = clean
        state["caption"] = f"YouTube search: {clean}"

    return open_browser_url(url)


def open_youtube_music_search(query: str):
    clean = query.strip()

    if not clean:
        return {
            "ok": False,
            "error": "Missing YouTube Music search query",
        }

    encoded = urllib.parse.quote_plus(clean)
    url = f"https://music.youtube.com/search?q={encoded}"

    with state_lock:
        state["youtube_status"] = "YouTube Music"
        state["youtube_last_query"] = clean
        state["caption"] = f"YouTube Music: {clean}"

    return open_browser_url(url)


def google_search_from_phone(query: str):
    clean = query.strip()

    if not clean:
        return {
            "ok": False,
            "error": "Missing Google search query",
        }

    encoded = urllib.parse.quote_plus(clean)
    url = f"https://www.google.com/search?q={encoded}"

    with state_lock:
        state["caption"] = f"Google search: {clean}"

    return open_browser_url(url)


def youtube_key(control_name: str):
    controls = {
        "play_pause": ["xdotool", "key", "space"],
        "youtube_play_pause": ["xdotool", "key", "k"],
        "select": ["xdotool", "key", "Return"],
        "tab": ["xdotool", "key", "Tab"],
        "shift_tab": ["xdotool", "key", "shift+Tab"],
        "up": ["xdotool", "key", "Up"],
        "down": ["xdotool", "key", "Down"],
        "left": ["xdotool", "key", "Left"],
        "right": ["xdotool", "key", "Right"],
        "rewind": ["xdotool", "key", "j"],
        "forward": ["xdotool", "key", "l"],
        "mute": ["xdotool", "key", "m"],
        "fullscreen": ["xdotool", "key", "f"],
        "escape": ["xdotool", "key", "Escape"],
        "volume_up": ["xdotool", "key", "XF86AudioRaiseVolume"],
        "volume_down": ["xdotool", "key", "XF86AudioLowerVolume"],
    }

    if control_name not in controls:
        return {
            "ok": False,
            "error": f"Unknown YouTube control: {control_name}",
        }

    focus_result = focus_browser()
    cmd_result = run_shell(controls[control_name])

    with state_lock:
        state["youtube_status"] = control_name

    return {
        "ok": cmd_result.get("ok", False),
        "focus": focus_result,
        "control": control_name,
        "cmd_result": cmd_result,
    }


def close_youtube():
    result1 = run_shell(["pkill", "-f", "firefox"])
    result2 = run_shell(["pkill", "-f", "chromium"])

    with state_lock:
        state["youtube_status"] = "closed"
        state["caption"] = "Browser closed. Returning to HUD."

    time.sleep(0.8)

    hud_result = return_to_hud()

    return {
        "ok": True,
        "firefox": result1,
        "chromium": result2,
        "return_to_hud": hud_result,
    }


def return_to_hud():
    result = focus_truevision()

    time.sleep(0.25)

    try:
        cv2.setWindowProperty(
            "TrueVision",
            cv2.WND_PROP_FULLSCREEN,
            cv2.WINDOW_FULLSCREEN,
        )
    except Exception as e:
        print("[PI] Could not force fullscreen on return:", e)

    with state_lock:
        state["youtube_status"] = "HUD"
        state["caption"] = "Returned to TrueVision HUD"

    return {
        "ok": True,
        "focus": result,
        "status": "Returned to TrueVision HUD",
    }


def shutdown_truevision():
    with state_lock:
        state["running"] = False

    return {
        "ok": True,
        "status": "TrueVision shutting down",
    }


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

    with state_lock:
        state["caption"] = "Opening Google Maps"

    return open_browser_url(url)


def open_music():
    url = "https://music.youtube.com"

    print("[PI] Opening music")

    with state_lock:
        state["youtube_status"] = "YouTube Music"
        state["caption"] = "Opening YouTube Music"

    return open_browser_url(url)


def open_video_call():
    url = "https://meet.google.com"

    print("[PI] Opening video call page")

    with state_lock:
        state["caption"] = "Opening Google Meet"

    return open_browser_url(url)


def fetch_weather():
    with state_lock:
        loc = state.get("location")
        location_label = state.get("location_label", "West Lafayette, IN")

    if loc and "lat" in loc and "lon" in loc:
        lat = loc["lat"]
        lon = loc["lon"]
    else:
        lat = DEFAULT_LAT
        lon = DEFAULT_LON
        location_label = "West Lafayette, IN"

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current_weather=true"
        "&temperature_unit=fahrenheit"
    )

    try:
        data = requests.get(url, timeout=10).json()
        current = data.get("current_weather", {})
        temp = current.get("temperature", None)

        if temp is None:
            raise RuntimeError(f"No temperature in response: {data}")

        temp_text = f"{float(temp):.0f}F"

        with state_lock:
            state["weather"] = temp_text
            state["weather_temp"] = temp_text
            state["weather_last_updated"] = time.time()
            state["location_label"] = location_label

        print("[PI] Weather:", temp_text, "|", location_label)

    except Exception as e:
        msg = f"Weather failed: {e}"

        with state_lock:
            state["weather"] = msg
            state["weather_temp"] = "--"

        print("[PI]", msg)


def weather_refresh_thread():
    time.sleep(3)

    while state["running"]:
        fetch_weather()

        for _ in range(300):
            if not state["running"]:
                return
            time.sleep(1)


def fetch_news():
    if not NEWS_API_KEY:
        msg = "News API key not set on Pi."

        with state_lock:
            state["news"] = msg
            state["caption"] = msg

        return

    url = (
        "https://newsapi.org/v2/top-headlines"
        f"?country=us&pageSize=3&apiKey={NEWS_API_KEY}"
    )

    try:
        data = requests.get(url, timeout=10).json()
        articles = data.get("articles", [])

        headlines = [a.get("title", "") for a in articles[:3]]
        msg = " | ".join(headlines)

        with state_lock:
            state["news"] = msg
            state["caption"] = "News: " + msg

        print("[PI] News:", msg)

    except Exception as e:
        msg = f"News failed: {e}"

        with state_lock:
            state["caption"] = msg

        print("[PI]", msg)


def fetch_summary():
    try:
        resp = requests.get(f"{DGX_HTTP_URL}/conversation_summary", timeout=60)
        data = resp.json()

        summary = data.get("summary", "")

        with state_lock:
            state["summary"] = summary
            state["caption"] = "Summary: " + summary

        print("[SUMMARY]", summary)

    except Exception as e:
        msg = f"Summary failed: {e}"

        with state_lock:
            state["caption"] = msg

        print("[PI]", msg)


def rename_face_on_dgx(old_name: str, new_name: str):
    try:
        resp = requests.post(
            f"{DGX_HTTP_URL}/rename_face",
            json={
                "old_name": old_name,
                "new_name": new_name,
            },
            timeout=10,
        )

        return resp.json()

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


def save_unknown_face_on_dgx(name: str):
    try:
        resp = requests.post(
            f"{DGX_HTTP_URL}/save_unknown_face",
            json={
                "name": name,
            },
            timeout=10,
        )

        return resp.json()

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


def set_language_on_dgx(language: str):
    try:
        resp = requests.post(
            f"{DGX_HTTP_URL}/set_language",
            json={
                "language": language,
            },
            timeout=10,
        )

        data = resp.json()

        if data.get("ok"):
            with state_lock:
                state["selected_language"] = data.get("active_language", language)

        return data

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


def add_reminder(text: str):
    clean = sanitize_hud_text(text.strip())

    if not clean:
        return {
            "ok": False,
            "error": "Missing reminder text",
        }

    with state_lock:
        state["reminders"].append(clean)
        state["reminders"] = state["reminders"][-5:]

    return {
        "ok": True,
        "reminders": state["reminders"],
    }


def clear_reminders():
    with state_lock:
        state["reminders"] = []

    return {
        "ok": True,
        "reminders": [],
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
        .yt { background: #ef4444; color: white; }
        .nav { background: #0ea5e9; color: white; }

        pre {
            text-align: left;
            background: #020617;
            padding: 14px;
            border-radius: 12px;
            overflow-x: auto;
            white-space: pre-wrap;
        }

        .section {
            border-top: 1px solid #334155;
            margin-top: 18px;
            padding-top: 14px;
        }

        #joystickBase {
            width: 240px;
            height: 240px;
            margin: 18px auto;
            border-radius: 50%;
            background: radial-gradient(circle, #1e293b 0%, #020617 70%);
            border: 3px solid #475569;
            position: relative;
            touch-action: none;
            user-select: none;
            box-shadow: 0 0 18px rgba(14, 165, 233, 0.35);
        }

        #joystickKnob {
            width: 76px;
            height: 76px;
            border-radius: 50%;
            background: #0ea5e9;
            border: 3px solid #bae6fd;
            position: absolute;
            left: 82px;
            top: 82px;
            box-shadow: 0 0 18px rgba(14, 165, 233, 0.75);
            transition: left 0.04s linear, top 0.04s linear;
        }
    </style>
</head>

<body>
    <h1>TrueVision</h1>
    <div class="small">Phone Controller</div>

    <button class="audio" onclick="setMode('audio')">Audio Mode</button>
    <button class="face" onclick="setMode('face')">Face Mode</button>
    <button class="dual" onclick="setMode('dual')">Dual Mode</button>

    <div class="section">
        <h3>HUD Background</h3>
        <button class="gray" onclick="setHudBackground('camera')">Camera Background</button>
        <button class="gray" onclick="setHudBackground('black')">Black HUD Background</button>
    </div>

    <div class="section">
        <h3>Audio Language</h3>
        <button class="gray" onclick="setLanguage('en')">English Captions</button>
        <button class="gray" onclick="setLanguage('es')">Spanish to English</button>
        <button class="gray" onclick="setLanguage('de')">German to English</button>
        <button class="gray" onclick="setLanguage('ar')">Arabic to English</button>
        <button class="gray" onclick="setLanguage('hi')">Hindi to English</button>
        <button class="gray" onclick="setLanguage('ur')">Urdu to English</button>
    </div>

    <div class="section">
        <h3>YouTube Remote</h3>
        <input id="youtubeQuery" placeholder="Search YouTube, example lo-fi music">

        <button class="yt" onclick="youtubeSearch()">Open YouTube Search</button>
        <button class="yt" onclick="youtubeMusicSearch()">Open YouTube Music</button>

        <button class="nav" onclick="youtubeControl('tab')">Next Item</button>
        <button class="nav" onclick="youtubeControl('shift_tab')">Previous Item</button>
        <button class="nav" onclick="youtubeControl('select')">Select / Open</button>

        <button class="gray" onclick="youtubeControl('youtube_play_pause')">Play / Pause</button>
        <button class="gray" onclick="youtubeControl('rewind')">Rewind</button>
        <button class="gray" onclick="youtubeControl('forward')">Forward</button>
        <button class="gray" onclick="youtubeControl('mute')">Mute</button>
        <button class="gray" onclick="youtubeControl('fullscreen')">YouTube Fullscreen</button>
        <button class="gray" onclick="youtubeControl('escape')">Exit Fullscreen / Back</button>
        <button class="gray" onclick="youtubeControl('volume_up')">Volume Up</button>
        <button class="gray" onclick="youtubeControl('volume_down')">Volume Down</button>

        <button class="feature" onclick="returnToHud()">Force Return to HUD</button>
        <button class="danger" onclick="closeYoutube()">Close Browser</button>
    </div>

    <div class="section">
        <h3>Pi Keyboard</h3>

        <input id="keyboardText" placeholder="Type text to Pi">
        <button class="feature" onclick="typeToPi()">Type to Active Window</button>

        <button class="gray" onclick="keyboardKey('enter')">Enter</button>
        <button class="gray" onclick="keyboardKey('backspace')">Backspace</button>
        <button class="gray" onclick="keyboardKey('space')">Space</button>
        <button class="gray" onclick="keyboardKey('escape')">Escape</button>
        <button class="gray" onclick="keyboardKey('tab')">Tab</button>
        <button class="gray" onclick="keyboardKey('ctrl_l')">Address Bar</button>
        <button class="gray" onclick="keyboardKey('ctrl_a')">Select All</button>

        <h3>Google Search</h3>
        <input id="googleQuery" placeholder="Search Google">
        <button class="feature" onclick="googleSearch()">Open Google Search</button>
    </div>

    <div class="section">
        <h3>Cursor Joystick</h3>

        <div id="joystickBase">
            <div id="joystickKnob"></div>
        </div>

        <div class="small">Drag the circle to move the cursor</div>

        <button class="gray" onclick="mouseClick(1)">Left Click</button>
        <button class="gray" onclick="mouseClick(3)">Right Click</button>
        <button class="gray" onclick="mouseScroll('up')">Scroll Up</button>
        <button class="gray" onclick="mouseScroll('down')">Scroll Down</button>
    </div>

    <div class="section">
        <h3>Location / Weather</h3>
        <button class="gray" onclick="sendLocation()">Use Phone GPS Location</button>
        <button class="gray" onclick="setWestLafayette()">Set West Lafayette</button>
        <button class="feature" onclick="feature('weather')">Refresh Weather</button>
    </div>

    <div class="section">
        <h3>Features</h3>
        <button class="feature" onclick="feature('maps')">Open Maps</button>
        <button class="feature" onclick="feature('news')">News</button>
        <button class="feature" onclick="feature('summary')">Summarize Conversation</button>
        <button class="feature" onclick="feature('music')">Music</button>
        <button class="feature" onclick="feature('call')">Video Call</button>
    </div>

    <div class="section">
        <h3>HUD Reminder</h3>
        <input id="reminderText" placeholder="Reminder text">
        <button class="gray" onclick="addReminder()">Add Reminder</button>
        <button class="danger" onclick="clearReminders()">Clear Reminders</button>
    </div>

    <div class="section">
        <h3>Save Current Unknown Face</h3>
        <input id="newUnknownName" placeholder="Name, example Aditya">
        <button class="gray" onclick="saveUnknownFace()">Save Current Unknown</button>
    </div>

    <div class="section">
        <h3>Rename Existing Face</h3>
        <input id="oldName" placeholder="Old name, example Aditya">
        <input id="newName" placeholder="New name, example Professor">
        <button class="gray" onclick="renameFace()">Rename Existing Face</button>
    </div>

    <div class="section">
        <button class="gray" onclick="refreshState()">Refresh State</button>
        <button class="danger" onclick="shutdownTrueVision()">Shutdown TrueVision</button>
    </div>

    <pre id="status">Ready</pre>

<script>
let joystickTimer = null;
let joystickActive = false;
let joystickMoveX = 0;
let joystickMoveY = 0;

async function setMode(mode) {
    const res = await fetch('/mode/' + mode, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function setHudBackground(mode) {
    const res = await fetch('/hud_background/' + mode, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function setLanguage(language) {
    const res = await fetch('/language/' + language, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function feature(name) {
    const res = await fetch('/feature/' + name, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function youtubeSearch() {
    const query = document.getElementById('youtubeQuery').value.trim();

    if (!query) {
        document.getElementById('status').innerText = 'Please enter a YouTube search';
        return;
    }

    const res = await fetch('/youtube/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: query})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function youtubeMusicSearch() {
    const query = document.getElementById('youtubeQuery').value.trim();

    if (!query) {
        document.getElementById('status').innerText = 'Please enter a YouTube Music search';
        return;
    }

    const res = await fetch('/youtube/music_search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: query})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function youtubeControl(name) {
    const res = await fetch('/youtube/control/' + name, {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function closeYoutube() {
    const res = await fetch('/youtube/close', {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function returnToHud() {
    const res = await fetch('/return_to_hud', {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function shutdownTrueVision() {
    const sure = confirm('Shutdown TrueVision on the Pi?');

    if (!sure) {
        return;
    }

    const res = await fetch('/shutdown', {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function typeToPi() {
    const text = document.getElementById('keyboardText').value;

    if (!text) {
        document.getElementById('status').innerText = 'Please enter text to type';
        return;
    }

    const res = await fetch('/keyboard/type', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function keyboardKey(name) {
    const res = await fetch('/keyboard/key/' + name, {
        method: 'POST'
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function googleSearch() {
    const query = document.getElementById('googleQuery').value.trim();

    if (!query) {
        document.getElementById('status').innerText = 'Please enter a Google search';
        return;
    }

    const res = await fetch('/google/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: query})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function mouseMove(dx, dy) {
    await fetch('/mouse/move', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            dx: dx,
            dy: dy
        })
    });
}

function resetJoystickKnob() {
    const knob = document.getElementById('joystickKnob');

    if (!knob) {
        return;
    }

    knob.style.left = '82px';
    knob.style.top = '82px';
}

function stopJoystick() {
    joystickActive = false;
    joystickMoveX = 0;
    joystickMoveY = 0;

    if (joystickTimer !== null) {
        clearInterval(joystickTimer);
        joystickTimer = null;
    }

    resetJoystickKnob();
}

function updateJoystickFromPoint(clientX, clientY) {
    const base = document.getElementById('joystickBase');
    const knob = document.getElementById('joystickKnob');

    if (!base || !knob) {
        return;
    }

    const rect = base.getBoundingClientRect();

    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;

    let dx = clientX - centerX;
    let dy = clientY - centerY;

    const maxRadius = rect.width / 2 - 42;
    const distance = Math.sqrt(dx * dx + dy * dy);

    if (distance > maxRadius) {
        dx = dx / distance * maxRadius;
        dy = dy / distance * maxRadius;
    }

    const knobCenterX = rect.width / 2 + dx;
    const knobCenterY = rect.height / 2 + dy;

    knob.style.left = `${knobCenterX - 38}px`;
    knob.style.top = `${knobCenterY - 38}px`;

    const normalizedX = dx / maxRadius;
    const normalizedY = dy / maxRadius;

    const maxSpeed = 34;

    joystickMoveX = Math.round(normalizedX * maxSpeed);
    joystickMoveY = Math.round(normalizedY * maxSpeed);
}

function startJoystickLoop() {
    if (joystickTimer !== null) {
        return;
    }

    joystickTimer = setInterval(function() {
        if (!joystickActive) {
            return;
        }

        if (joystickMoveX !== 0 || joystickMoveY !== 0) {
            mouseMove(joystickMoveX, joystickMoveY);
        }
    }, 45);
}

function setupJoystick() {
    const base = document.getElementById('joystickBase');

    if (!base) {
        return;
    }

    base.addEventListener('touchstart', function(e) {
        e.preventDefault();

        joystickActive = true;

        if (e.touches.length > 0) {
            updateJoystickFromPoint(
                e.touches[0].clientX,
                e.touches[0].clientY
            );
        }

        startJoystickLoop();
    }, {passive: false});

    base.addEventListener('touchmove', function(e) {
        e.preventDefault();

        if (!joystickActive) {
            return;
        }

        if (e.touches.length > 0) {
            updateJoystickFromPoint(
                e.touches[0].clientX,
                e.touches[0].clientY
            );
        }
    }, {passive: false});

    base.addEventListener('touchend', function(e) {
        e.preventDefault();
        stopJoystick();
    }, {passive: false});

    base.addEventListener('touchcancel', function(e) {
        e.preventDefault();
        stopJoystick();
    }, {passive: false});

    base.addEventListener('mousedown', function(e) {
        e.preventDefault();

        joystickActive = true;

        updateJoystickFromPoint(e.clientX, e.clientY);
        startJoystickLoop();
    });

    window.addEventListener('mousemove', function(e) {
        if (!joystickActive) {
            return;
        }

        updateJoystickFromPoint(e.clientX, e.clientY);
    });

    window.addEventListener('mouseup', function(e) {
        if (!joystickActive) {
            return;
        }

        stopJoystick();
    });
}

async function mouseClick(button) {
    const res = await fetch('/mouse/click', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({button: button})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function mouseScroll(direction) {
    const res = await fetch('/mouse/scroll/' + direction, {
        method: 'POST'
    });

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

async function setWestLafayette() {
    const res = await fetch('/location/west_lafayette', {
        method: 'POST'
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function addReminder() {
    const text = document.getElementById('reminderText').value.trim();

    if (!text) {
        document.getElementById('status').innerText = 'Please enter reminder text';
        return;
    }

    const res = await fetch('/reminder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function clearReminders() {
    const res = await fetch('/reminders/clear', {method: 'POST'});
    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
}

async function saveUnknownFace() {
    const name = document.getElementById('newUnknownName').value.trim();

    if (!name) {
        document.getElementById('status').innerText = 'Please enter a name';
        return;
    }

    const res = await fetch('/save_unknown_face', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name})
    });

    const data = await res.json();
    document.getElementById('status').innerText = JSON.stringify(data, null, 2);
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

window.addEventListener('load', setupJoystick);
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
            "error": "Unknown mode",
        }

    with state_lock:
        state["mode"] = MODE_FROM_NAME[mode_name]

    return {
        "ok": True,
        "mode": mode_name,
    }


@phone_app.post("/hud_background/{background_mode}")
def set_hud_background(background_mode: str):
    if background_mode == "camera":
        with state_lock:
            state["hud_camera_background"] = True

        return {
            "ok": True,
            "hud_camera_background": True,
            "mode": "camera",
        }

    if background_mode == "black":
        with state_lock:
            state["hud_camera_background"] = False

        return {
            "ok": True,
            "hud_camera_background": False,
            "mode": "black",
        }

    return {
        "ok": False,
        "error": "Unknown HUD background mode. Use camera or black.",
    }


@phone_app.post("/language/{language}")
def set_language(language: str):
    return set_language_on_dgx(language)


@phone_app.post("/feature/{feature_name}")
def run_feature(feature_name: str):
    if feature_name == "maps":
        threading.Thread(target=open_maps, daemon=True).start()

    elif feature_name == "weather":
        threading.Thread(target=fetch_weather, daemon=True).start()

    elif feature_name == "news":
        threading.Thread(target=fetch_news, daemon=True).start()

    elif feature_name == "summary":
        threading.Thread(target=fetch_summary, daemon=True).start()

    elif feature_name == "music":
        threading.Thread(target=open_music, daemon=True).start()

    elif feature_name == "call":
        threading.Thread(target=open_video_call, daemon=True).start()

    else:
        return {
            "ok": False,
            "error": "Unknown feature",
        }

    return {
        "ok": True,
        "feature": feature_name,
    }


@phone_app.post("/youtube/search")
def youtube_search(payload: Dict[str, str]):
    query = payload.get("query", "").strip()
    return open_youtube_search(query)


@phone_app.post("/youtube/music_search")
def youtube_music_search(payload: Dict[str, str]):
    query = payload.get("query", "").strip()
    return open_youtube_music_search(query)


@phone_app.post("/youtube/control/{control_name}")
def youtube_control(control_name: str):
    return youtube_key(control_name)


@phone_app.post("/youtube/close")
def youtube_close():
    return close_youtube()


@phone_app.post("/google/search")
def google_search_route(payload: Dict[str, str]):
    query = payload.get("query", "").strip()
    return google_search_from_phone(query)


@phone_app.post("/keyboard/type")
def keyboard_type_route(payload: Dict[str, str]):
    text = payload.get("text", "")
    return keyboard_type_text(text)


@phone_app.post("/keyboard/key/{key_name}")
def keyboard_key_route(key_name: str):
    return keyboard_key(key_name)


@phone_app.post("/return_to_hud")
def return_to_hud_route():
    return return_to_hud()


@phone_app.post("/shutdown")
def shutdown_route():
    return shutdown_truevision()


@phone_app.post("/mouse/move")
def mouse_move_route(payload: Dict[str, int]):
    dx = payload.get("dx", 0)
    dy = payload.get("dy", 0)

    return mouse_move(dx, dy)


@phone_app.post("/mouse/click")
def mouse_click_route(payload: Dict[str, int]):
    button = payload.get("button", 1)

    return mouse_click(button)


@phone_app.post("/mouse/scroll/{direction}")
def mouse_scroll_route(direction: str):
    return mouse_scroll(direction)


@phone_app.post("/mouse/drag_start")
def mouse_drag_start_route():
    return mouse_drag_start()


@phone_app.post("/mouse/drag_end")
def mouse_drag_end_route():
    return mouse_drag_end()


@phone_app.post("/location")
def update_location(payload: Dict[str, float]):
    lat = float(payload["lat"])
    lon = float(payload["lon"])

    with state_lock:
        state["location"] = {
            "lat": lat,
            "lon": lon,
        }
        state["location_label"] = "Phone GPS"

    threading.Thread(target=fetch_weather, daemon=True).start()

    return {
        "ok": True,
        "location": state["location"],
        "location_label": "Phone GPS",
        "weather_update": "started",
    }


@phone_app.post("/location/west_lafayette")
def set_west_lafayette_location():
    with state_lock:
        state["location"] = {
            "lat": DEFAULT_LAT,
            "lon": DEFAULT_LON,
        }
        state["location_label"] = "West Lafayette, IN"

    threading.Thread(target=fetch_weather, daemon=True).start()

    return {
        "ok": True,
        "location": state["location"],
        "location_label": "West Lafayette, IN",
        "weather_update": "started",
    }


@phone_app.post("/reminder")
def add_reminder_route(payload: Dict[str, str]):
    text = payload.get("text", "")
    return add_reminder(text)


@phone_app.post("/reminders/clear")
def clear_reminders_route():
    return clear_reminders()


@phone_app.post("/rename_face")
def rename_face(payload: Dict[str, str]):
    old_name = payload.get("old_name", "").strip()
    new_name = payload.get("new_name", "").strip()

    if not old_name or not new_name:
        return {
            "ok": False,
            "error": "Missing old_name or new_name",
        }

    return rename_face_on_dgx(old_name, new_name)


@phone_app.post("/save_unknown_face")
def save_unknown_face(payload: Dict[str, str]):
    name = payload.get("name", "").strip()

    if not name:
        return {
            "ok": False,
            "error": "Missing name",
        }

    return save_unknown_face_on_dgx(name)


@phone_app.get("/state")
def get_state():
    with state_lock:
        return {
            "running": state["running"],
            "mode": MODE_NAMES.get(state["mode"], "UNKNOWN"),
            "caption": state["caption"],
            "last_original_text": state["last_original_text"],
            "last_language": state["last_language"],
            "selected_language": state["selected_language"],
            "last_task": state["last_task"],
            "summary": state["summary"],
            "faces": state["faces"],
            "weather": state["weather"],
            "weather_temp": state["weather_temp"],
            "weather_last_updated": state["weather_last_updated"],
            "news": state["news"],
            "location": state["location"],
            "location_label": state["location_label"],
            "hud_camera_background": state["hud_camera_background"],
            "reminders": state["reminders"],
            "telegram_notifications": state["telegram_notifications"],
            "youtube_status": state["youtube_status"],
            "youtube_last_query": state["youtube_last_query"],
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
        log_level="warning",
    )


# =========================
# Main
# =========================

def main():
    print("[PI] Starting TrueVision Pi App")
    print("[PI] DGX HTTP:", DGX_HTTP_URL)
    print("[PI] DGX Audio WS:", DGX_AUDIO_WS_URL)
    print("[PI] DGX Face WS:", DGX_FACE_WS_URL)
    print("[PI] Display:", DISPLAY_WIDTH, "x", DISPLAY_HEIGHT)

    threads = [
        threading.Thread(target=uart_thread, daemon=True),
        threading.Thread(target=dgx_audio_thread, daemon=True),
        threading.Thread(target=dgx_face_thread, daemon=True),
        threading.Thread(target=telegram_notifications_thread, daemon=True),
        threading.Thread(target=weather_refresh_thread, daemon=True),
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