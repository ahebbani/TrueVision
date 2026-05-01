import os
import cv2
import json
import time
import pickle
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Any, List

import numpy as np
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from faster_whisper import WhisperModel


# =========================
# Optional face_recognition
# =========================

try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
    print("[DGX] face_recognition loaded")
except Exception as e:
    FACE_RECOGNITION_AVAILABLE = False
    print("[DGX] face_recognition unavailable:", e)


# =========================
# App
# =========================

app = FastAPI(title="TrueVision DGX Server")


# =========================
# Config
# =========================

WHISPER_MODEL_SIZE = "base"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"

# Telegram hardcoded config.
# Paste your real bot token below.
# I am not reprinting the full token here for safety.
TELEGRAM_BOT_TOKEN = "8651924169:AAFhja-ZRfqCEV3Q6yy4gZAM6tCIthpNLDg"

# Same chat for all Assistant commands.
TELEGRAM_CHAT_ID = "-5141486260"

FACE_DB_PATH = Path("face_memory.pkl")
CONVERSATION_LOG_PATH = Path("conversation_log.txt")

FACE_MATCH_TOLERANCE = 0.50
FACE_COUNT_COOLDOWN_SECONDS = 10

conversation_buffer: List[str] = []

# Phone-selected language mode.
# en = English transcription only
# es = Spanish to English
# de = German to English
# ar = Arabic to English
# hi = Hindi to English
# ur = Urdu to English
ACTIVE_LANGUAGE = "en"
SUPPORTED_LANGUAGES = {"en", "es", "de", "ar", "hi", "ur"}


# =========================
# Load Whisper
# =========================

print(f"[DGX] Loading Whisper model: {WHISPER_MODEL_SIZE}")
print(f"[DGX] Whisper device: {WHISPER_DEVICE}")
print(f"[DGX] Whisper compute type: {WHISPER_COMPUTE_TYPE}")

whisper_model = WhisperModel(
    WHISPER_MODEL_SIZE,
    device=WHISPER_DEVICE,
    compute_type=WHISPER_COMPUTE_TYPE
)

print("[DGX] Whisper ready")


# =========================
# API Models
# =========================

class TelegramPayload(BaseModel):
    command: str


class SummaryPayload(BaseModel):
    text: str


class RenameFacePayload(BaseModel):
    old_name: str
    new_name: str


class SaveUnknownFacePayload(BaseModel):
    name: str


class LanguagePayload(BaseModel):
    language: str


# =========================
# Face Memory
# =========================

class FaceMemory:
    def __init__(self, path: Path):
        self.path = path
        self.known_encodings = []
        self.known_names = []
        self.counts: Dict[str, int] = {}
        self.first_seen: Dict[str, float] = {}
        self.last_seen: Dict[str, float] = {}
        self.next_person_id = 1
        self.load()

    def load(self):
        if not self.path.exists():
            print("[DGX] No face DB yet")
            return

        try:
            data = pickle.loads(self.path.read_bytes())

            self.known_encodings = data.get("known_encodings", [])
            self.known_names = data.get("known_names", [])
            self.counts = data.get("counts", {})
            self.first_seen = data.get("first_seen", {})
            self.last_seen = data.get("last_seen", {})
            self.next_person_id = data.get("next_person_id", 1)

            print(f"[DGX] Loaded {len(self.known_names)} known faces")

        except Exception as e:
            print("[DGX] Failed to load face memory:", e)

    def save(self):
        data = {
            "known_encodings": self.known_encodings,
            "known_names": self.known_names,
            "counts": self.counts,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "next_person_id": self.next_person_id,
        }

        self.path.write_bytes(pickle.dumps(data))

    def recognize(self, encoding):
        """
        Recognize known face only.
        Unknown faces are NOT saved automatically.
        """
        if len(self.known_encodings) == 0:
            return None

        matches = face_recognition.compare_faces(
            self.known_encodings,
            encoding,
            tolerance=FACE_MATCH_TOLERANCE
        )

        distances = face_recognition.face_distance(
            self.known_encodings,
            encoding
        )

        if len(distances) > 0:
            best_idx = int(np.argmin(distances))

            if matches[best_idx]:
                name = self.known_names[best_idx]
                self.mark_seen(name)
                return name

        return None

    def save_new_person(self, name: str, encoding) -> str:
        now = time.time()
        clean_name = name.strip()

        if not clean_name:
            clean_name = f"Person_{self.next_person_id:03d}"

        self.known_encodings.append(encoding)
        self.known_names.append(clean_name)

        self.counts[clean_name] = 1
        self.first_seen[clean_name] = now
        self.last_seen[clean_name] = now

        self.next_person_id += 1
        self.save()

        print(f"[DGX] Manually saved new face: {clean_name}")

        return clean_name

    def mark_seen(self, name: str):
        now = time.time()
        last = self.last_seen.get(name, 0)

        if now - last >= FACE_COUNT_COOLDOWN_SECONDS:
            self.counts[name] = self.counts.get(name, 0) + 1

        self.last_seen[name] = now
        self.save()

    def rename(self, old_name: str, new_name: str) -> bool:
        old_name = old_name.strip()
        new_name = new_name.strip()

        if not old_name or not new_name:
            return False

        found = False

        for i, name in enumerate(self.known_names):
            if name == old_name:
                self.known_names[i] = new_name
                found = True

        if not found:
            return False

        if old_name in self.counts:
            self.counts[new_name] = self.counts.pop(old_name)

        if old_name in self.first_seen:
            self.first_seen[new_name] = self.first_seen.pop(old_name)

        if old_name in self.last_seen:
            self.last_seen[new_name] = self.last_seen.pop(old_name)

        self.save()
        return True

    def info(self, name: str) -> Dict[str, Any]:
        return {
            "name": name,
            "seen_count": self.counts.get(name, 1),
            "first_seen": self.first_seen.get(name),
            "last_seen": self.last_seen.get(name),
        }


face_memory = FaceMemory(FACE_DB_PATH)

latest_unknown_face: Dict[str, Any] = {
    "encoding": None,
    "updated_at": 0.0
}


# =========================
# Telegram
# =========================

def send_telegram_message(text: str) -> Dict[str, Any]:
    """
    Sends every Assistant command message to the same Telegram chat.
    """

    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        return {
            "ok": False,
            "error": "Missing Telegram bot token in app.py"
        }

    if not TELEGRAM_CHAT_ID:
        return {
            "ok": False,
            "error": "Missing TELEGRAM_CHAT_ID in app.py"
        }

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    try:
        response = requests.post(
            f"{base_url}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text
            },
            timeout=20
        )

        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            return {
                "ok": False,
                "error": f"Telegram API error: {data}"
            }

        return {
            "ok": True,
            "message_id": data["result"]["message_id"],
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "raw": data
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def extract_message_after_assistant(text: str) -> Dict[str, Any]:
    """
    Finds 'assistant' anywhere in the transcript and extracts the message.

    Supported examples:
      Assistant I will be late
      Assistant send message I will be late
      Assistant send message to hebbani I will be late
      Assistant send telegram TrueVision demo is working
      Hey assistant send message to hebbani I will be late
    """

    clean = text.strip()
    lowered = clean.lower()

    if "assistant" not in lowered:
        return {
            "is_command": False
        }

    assistant_idx = lowered.find("assistant")
    body = clean[assistant_idx + len("assistant"):].strip()
    body_lower = body.lower()

    if not body:
        return {
            "is_command": True,
            "action": "telegram",
            "message": "",
            "error": "Assistant heard, but no message was provided."
        }

    # Remove common command phrases.
    prefixes = [
        "send message to ",
        "send telegram to ",
        "send text to ",
        "message to ",
        "telegram to ",
        "text to ",
        "send message ",
        "send telegram ",
        "send text ",
        "message ",
        "telegram ",
        "text ",
        "send ",
    ]

    for prefix in prefixes:
        if body_lower.startswith(prefix):
            body = body[len(prefix):].strip()
            body_lower = body.lower()
            break

    # If phrase was "to hebbani I will be late", remove recipient.
    if body_lower.startswith("to "):
        body = body[3:].strip()
        parts = body.split(maxsplit=1)

        if len(parts) == 2:
            body = parts[1].strip()

    # If phrase is "hebbani I will be late", remove first word if it looks like a recipient.
    parts = body.split(maxsplit=1)

    if len(parts) == 2:
        possible_recipient = parts[0].strip().lower()
        known_recipient_words = {
            "hebbani",
            "group",
            "aditya",
            "zohaib",
            "mom",
            "dad",
            "team"
        }

        if possible_recipient in known_recipient_words:
            body = parts[1].strip()

    message = body.strip()

    if not message:
        return {
            "is_command": True,
            "action": "telegram",
            "message": "",
            "error": "No Telegram message found after Assistant command."
        }

    return {
        "is_command": True,
        "action": "telegram",
        "message": message
    }


# =========================
# Summary
# =========================

def summarize_with_ollama(text: str) -> str:
    if not text.strip():
        return "No conversation captured yet."

    prompt = f"""
Summarize this conversation clearly.

Include:
1. Main topic
2. Important details
3. Any action items

Conversation:
{text}
"""

    try:
        result = subprocess.run(
            ["ollama", "run", "llama3.1:8b", prompt],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

    except Exception as e:
        print("[DGX] Ollama failed:", e)

    words = text.split()
    return " ".join(words[:120]) + ("..." if len(words) > 120 else "")


# =========================
# Audio Transcription / Translation
# =========================

def transcribe_wav_file(wav_path: str) -> Dict[str, Any]:
    global ACTIVE_LANGUAGE

    language = ACTIVE_LANGUAGE

    if language not in SUPPORTED_LANGUAGES:
        language = "en"

    if language == "en":
        task_mode = "transcribe"
    else:
        task_mode = "translate"

    segments, info = whisper_model.transcribe(
        wav_path,
        beam_size=3,
        vad_filter=True,
        task=task_mode,
        language=language
    )

    final_text = " ".join(seg.text.strip() for seg in segments).strip()
    detected_language = info.language or language

    if language == "en":
        task = "transcribe_en"
    else:
        task = f"translate_{language}_to_en"

    print(f"[DGX TRANSCRIPT] selected={language} detected={detected_language} task={task}: {final_text}")

    command = extract_message_after_assistant(final_text)

    telegram_result = None

    if command.get("is_command"):
        print(f"[DGX ASSISTANT COMMAND] {command}")

        msg = command.get("message", "").strip()

        if command.get("error"):
            telegram_result = {
                "ok": False,
                "error": command.get("error")
            }
            print(f"[DGX TELEGRAM ERROR] {telegram_result}")

        elif msg:
            telegram_result = send_telegram_message(msg)
            print(f"[DGX TELEGRAM RESULT] {telegram_result}")

        else:
            telegram_result = {
                "ok": False,
                "error": "No Telegram message provided"
            }
            print(f"[DGX TELEGRAM ERROR] {telegram_result}")

    if final_text:
        conversation_buffer.append(final_text)

        with open(CONVERSATION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {final_text}\n")

    return {
        "text": final_text,
        "original_text": final_text,
        "detected_language": detected_language,
        "selected_language": language,
        "task": task,
        "command": command,
        "telegram_result": telegram_result
    }


# =========================
# Face Recognition
# =========================

def recognize_faces_from_jpeg(jpeg_bytes: bytes) -> Dict[str, Any]:
    if not FACE_RECOGNITION_AVAILABLE:
        return {
            "faces": [],
            "error": "face_recognition is not installed on DGX"
        }

    np_arr = np.frombuffer(jpeg_bytes, np.uint8)
    frame_bgr = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if frame_bgr is None:
        return {
            "faces": [],
            "error": "Could not decode JPEG"
        }

    h, w = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

    locations = face_recognition.face_locations(rgb, model="hog")
    encodings = face_recognition.face_encodings(rgb, locations)

    faces = []

    for location, encoding in zip(locations, encodings):
        top, right, bottom, left = location

        known_name = face_memory.recognize(encoding)

        if known_name is not None:
            info = face_memory.info(known_name)

            faces.append({
                "name": known_name,
                "known": True,
                "pending_face_id": None,
                "box": [left, top, right, bottom],
                "seen_count": info["seen_count"],
                "first_seen": info["first_seen"],
                "last_seen": info["last_seen"],
                "frame_width": w,
                "frame_height": h
            })

        else:
            latest_unknown_face["encoding"] = encoding
            latest_unknown_face["updated_at"] = time.time()

            faces.append({
                "name": "Unknown",
                "known": False,
                "pending_face_id": "latest_unknown",
                "box": [left, top, right, bottom],
                "seen_count": None,
                "first_seen": None,
                "last_seen": None,
                "frame_width": w,
                "frame_height": h
            })

    return {
        "faces": faces,
        "frame_width": w,
        "frame_height": h,
        "latest_unknown_age": time.time() - latest_unknown_face["updated_at"]
        if latest_unknown_face["encoding"] is not None else None
    }


# =========================
# HTTP Routes
# =========================

@app.get("/health")
def health():
    return {
        "ok": True,
        "server": "TrueVision DGX",
        "whisper_model": WHISPER_MODEL_SIZE,
        "whisper_device": WHISPER_DEVICE,
        "whisper_compute_type": WHISPER_COMPUTE_TYPE,
        "face_recognition": FACE_RECOGNITION_AVAILABLE,
        "known_faces": len(face_memory.known_names),
        "active_language": ACTIVE_LANGUAGE,
        "supported_languages": sorted(list(SUPPORTED_LANGUAGES)),
        "telegram_enabled": bool(
            TELEGRAM_BOT_TOKEN
            and TELEGRAM_BOT_TOKEN != "PASTE_YOUR_BOT_TOKEN_HERE"
            and TELEGRAM_CHAT_ID
        ),
        "latest_unknown_available": latest_unknown_face["encoding"] is not None
    }


@app.post("/set_language")
def set_language(payload: LanguagePayload):
    global ACTIVE_LANGUAGE

    lang = payload.language.strip().lower()

    if lang not in SUPPORTED_LANGUAGES:
        return {
            "ok": False,
            "error": f"Unsupported language: {lang}",
            "supported": sorted(list(SUPPORTED_LANGUAGES))
        }

    ACTIVE_LANGUAGE = lang

    return {
        "ok": True,
        "active_language": ACTIVE_LANGUAGE
    }


@app.get("/language")
def get_language():
    return {
        "active_language": ACTIVE_LANGUAGE,
        "supported": sorted(list(SUPPORTED_LANGUAGES))
    }


@app.post("/telegram")
def telegram(payload: TelegramPayload):
    """
    Direct endpoint for testing Telegram.
    Example:
      curl -X POST http://127.0.0.1:8008/telegram \
        -H "Content-Type: application/json" \
        -d '{"command":"Hello from TrueVision"}'
    """

    result = send_telegram_message(payload.command)

    return {
        "ok": result.get("ok", False),
        "telegram_result": result
    }


@app.post("/summarize")
def summarize(payload: SummaryPayload):
    return {
        "summary": summarize_with_ollama(payload.text)
    }


@app.get("/conversation_summary")
def conversation_summary():
    text = "\n".join(conversation_buffer[-80:])

    return {
        "summary": summarize_with_ollama(text)
    }


@app.get("/faces")
def get_faces():
    return {
        "known_names": face_memory.known_names,
        "counts": face_memory.counts,
        "first_seen": face_memory.first_seen,
        "last_seen": face_memory.last_seen,
        "latest_unknown_available": latest_unknown_face["encoding"] is not None,
        "latest_unknown_age": time.time() - latest_unknown_face["updated_at"]
        if latest_unknown_face["encoding"] is not None else None
    }


@app.post("/rename_face")
def rename_face(payload: RenameFacePayload):
    ok = face_memory.rename(payload.old_name, payload.new_name)

    return {
        "ok": ok,
        "old_name": payload.old_name,
        "new_name": payload.new_name
    }


@app.post("/save_unknown_face")
def save_unknown_face(payload: SaveUnknownFacePayload):
    name = payload.name.strip()

    if not name:
        return {
            "ok": False,
            "error": "Missing name"
        }

    encoding = latest_unknown_face.get("encoding")
    updated_at = latest_unknown_face.get("updated_at", 0)

    if encoding is None:
        return {
            "ok": False,
            "error": "No unknown face is currently visible. Stand in front of the camera first."
        }

    if time.time() - updated_at > 10:
        return {
            "ok": False,
            "error": "The unknown face is too old. Stand in front of the camera again and press save."
        }

    saved_name = face_memory.save_new_person(name, encoding)

    latest_unknown_face["encoding"] = None
    latest_unknown_face["updated_at"] = 0

    return {
        "ok": True,
        "saved_name": saved_name
    }


# =========================
# WebSockets
# =========================

@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    print("[DGX] Pi connected to audio WebSocket")

    try:
        while True:
            wav_bytes = await ws.receive_bytes()

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_bytes)
                wav_path = tmp.name

            try:
                result = transcribe_wav_file(wav_path)
                await ws.send_text(json.dumps(result))

            except Exception as e:
                print("[DGX] Audio error:", e)
                await ws.send_text(json.dumps({
                    "error": str(e)
                }))

            finally:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

    except WebSocketDisconnect:
        print("[DGX] Audio WebSocket disconnected")


@app.websocket("/ws/face")
async def ws_face(ws: WebSocket):
    await ws.accept()
    print("[DGX] Pi connected to face WebSocket")

    try:
        while True:
            jpeg_bytes = await ws.receive_bytes()

            try:
                result = recognize_faces_from_jpeg(jpeg_bytes)
                await ws.send_text(json.dumps(result))

            except Exception as e:
                print("[DGX] Face error:", e)
                await ws.send_text(json.dumps({
                    "faces": [],
                    "error": str(e)
                }))

    except WebSocketDisconnect:
        print("[DGX] Face WebSocket disconnected")