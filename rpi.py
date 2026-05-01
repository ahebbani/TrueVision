import os
import cv2
import json
import time
import wave
import queue
import serial
import threading
import subprocess
import webbrowser
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
FACE_SEND_WIDTH = 640
JPEG_QUALITY = 70

DISPLAY_WIDTH = 1280
DISPLAY_HEIGHT = 720

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
    "last_task": "",
    "last_command": {},
    "summary": "",

    "faces": [],
    "known_faces": {},
    "last_face_error": "",

    "weather": "",
    "news": "",
    "location": None,

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
                    task = data.get("task", "")

                    if text:
                        with state_lock:
                            state["caption"] = text
                            state["last_original_text"] = original
                            state["last_language"] = language
                            state["last_task"] = task
                            state["last_command"] = data.get("command", {})

                        print("[CAPTION]", text)
                        print("[LANG]", language, "|", task)

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
# Drawing
# =========================

def wrap_text(text: str, max_chars: int = 64) -> List[str]:
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


def draw_caption(frame, caption: str):
    if not caption:
        return frame

    h, w = frame.shape[:2]

    overlay = frame.copy()
    cv2.rectangle(overlay, (20, h - 150), (w - 20, h - 20), (0, 0, 0), -1)
    frame = cv2.addWeighted(overlay, 0.60, frame, 0.40, 0)

    lines = wrap_text(caption, max_chars=70)

    y = h - 108

    for line in lines[:3]:
        cv2.putText(
            frame,
            line,
            (40, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.78,
            (255, 255, 255),
            2,
            cv2.LINE_AA
        )
        y += 35

    return frame


def draw_status(frame):
    with state_lock:
        mode = state["mode"]
        uart_connected = state["uart_connected"]
        audio_connected = state["dgx_audio_connected"]
        face_connected = state["dgx_face_connected"]
        language = state["last_language"]
        task = state["last_task"]

    mode_name = MODE_NAMES.get(mode, "UNKNOWN")

    text = (
        f"Mode: {mode_name} | "
        f"UART: {'OK' if uart_connected else 'NO'} | "
        f"Audio DGX: {'OK' if audio_connected else 'NO'} | "
        f"Face DGX: {'OK' if face_connected else 'NO'}"
    )

    cv2.rectangle(frame, (0, 0), (frame.shape[1], 42), (0, 0, 0), -1)

    cv2.putText(
        frame,
        text,
        (15, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (0, 255, 255),
        2,
        cv2.LINE_AA
    )

    if language:
        lang_text = f"Language: {language}"
        if task:
            lang_text += f" | {task}"

        cv2.putText(
            frame,
            lang_text,
            (15, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (0, 255, 255),
            2,
            cv2.LINE_AA
        )

    return frame


def draw_faces(frame):
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
            label = f"{name} | seen {count}x"
        else:
            label = "Unknown | save from phone"

        cv2.rectangle(frame, (left, top), (right, bottom), (0, 255, 0), 2)

        label_y = max(top - 10, 30)
        label_width = min(520, max(230, len(label) * 13))

        cv2.rectangle(
            frame,
            (left, label_y - 28),
            (left + label_width, label_y + 7),
            (0, 255, 0),
            -1
        )

        cv2.putText(
            frame,
            label,
            (left + 6, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            (0, 0, 0),
            2,
            cv2.LINE_AA
        )

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

    last_face_send = 0

    while state["running"]:
        ret, frame = cap.read()

        if not ret or frame is None:
            print("[PI] Camera frame failed")
            time.sleep(0.05)
            continue

        frame = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))

        with state_lock:
            mode = state["mode"]
            caption = state["caption"]

        last_face_send = maybe_send_frame_to_dgx(frame, last_face_send)

        if mode in [MODE_FACE, MODE_DUAL]:
            frame = draw_faces(frame)

        if mode in [MODE_AUDIO, MODE_DUAL]:
            frame = draw_caption(frame, caption)

        frame = draw_status(frame)

        cv2.imshow("TrueVision", frame)

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

        elif key == ord("d"):
            with state_lock:
                state["mode"] = MODE_DUAL

        elif key == ord("m"):
            threading.Thread(target=open_maps, daemon=True).start()

        elif key == ord("s"):
            threading.Thread(target=fetch_summary, daemon=True).start()

    cap.release()
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

        print("[PI]", msg)

    except Exception as e:
        msg = f"Weather failed: {e}"

        with state_lock:
            state["caption"] = msg

        print("[PI]", msg)


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
        msg = "News: " + " | ".join(headlines)

        with state_lock:
            state["news"] = msg
            state["caption"] = msg

        print("[PI]", msg)

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


def save_unknown_face_on_dgx(name: str):
    with state_lock:
        faces = list(state["faces"])

    pending_face_id = None

    for face in faces:
        if not face.get("known", True) and face.get("pending_face_id"):
            pending_face_id = face["pending_face_id"]
            break

    if pending_face_id is None:
        return {
            "ok": False,
            "error": "No unknown face currently visible. Stand in front of the camera first."
        }

    try:
        resp = requests.post(
            f"{DGX_HTTP_URL}/save_unknown_face",
            json={
                "pending_face_id": pending_face_id,
                "name": name
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

    <h3>Save Current Unknown Face</h3>
    <input id="newUnknownName" placeholder="Name, example Aditya">
    <button class="gray" onclick="saveUnknownFace()">Save Current Unknown</button>

    <hr>

    <h3>Rename Existing Face</h3>
    <input id="oldName" placeholder="Old name, example Aditya">
    <input id="newName" placeholder="New name, example Professor">
    <button class="gray" onclick="renameFace()">Rename Existing Face</button>

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

async function saveUnknownFace() {
    const name = document.getElementById('newUnknownName').value.trim();

    if (!name) {
        document.getElementById('status').innerText = 'Please enter a name';
        return;
    }

    const res = await fetch('/save_unknown_face', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            name: name
        })
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

    with state_lock:
        state["mode"] = MODE_FROM_NAME[mode_name]

    return {
        "ok": True,
        "mode": mode_name
    }


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

    return result


@phone_app.post("/save_unknown_face")
def save_unknown_face(payload: Dict[str, str]):
    name = payload.get("name", "").strip()

    if not name:
        return {
            "ok": False,
            "error": "Missing name"
        }

    result = save_unknown_face_on_dgx(name)

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
            "last_task": state["last_task"],
            "summary": state["summary"],
            "faces": state["faces"],
            "weather": state["weather"],
            "news": state["news"],
            "location": state["location"],
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