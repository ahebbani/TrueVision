import socket, struct, json, re
import numpy as np
import requests
from faster_whisper import WhisperModel

# ---- UDP framing ----
MAGIC = b"AUD0"
PORT = 5005
SAMPLES_PER_PACKET = 256
EXPECTED_PAYLOAD = SAMPLES_PER_PACKET * 2  # 512 bytes

# ---- Audio ----
SAMPLE_RATE = 16000
CHUNK_SEC = 1.0  # <-- CHANGED from 3.0 to 1.0 for responsiveness

ALLOWED_LANGS = ["en", "hi", "tr"]
LANG_NAME = {"en": "English", "hi": "Hindi", "tr": "Turkish"}

# ---- Ollama ----
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3.2:1b"  # <-- from your `ollama list`

def call_ollama(prompt: str, timeout_s: int = 25) -> str:
    r = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=timeout_s
    )
    r.raise_for_status()
    return r.json()["response"].strip()

# ---- Routing ----
WAKE_WORDS = ("assistant", "hey assistant", "computer", "jarvis")
COMMAND_VERBS = ("show", "open", "close", "start", "stop", "increase", "decrease", "set", "toggle", "clear")

ALLOWED_COMMANDS = {
    "set_font_size",
    "set_brightness",
    "clear_screen",
    "help"
}

def should_route(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return False
    if any(w in t for w in WAKE_WORDS):
        return True
    if t.endswith("?"):
        return True
    if any(t.startswith(v + " ") for v in COMMAND_VERBS):
        return True
    return False

def extract_json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def llm_route(text: str) -> dict:
    system = (
        "You are a command router. Output ONLY valid JSON.\n"
        "Schema: {\"type\":\"command|query|display_only|ignore\","
        "\"intent\":\"...\",\"args\":{},\"response_text\":\"\"}\n"
        f"If type=command, intent MUST be one of: {', '.join(sorted(ALLOWED_COMMANDS))}.\n"
        "For commands, extract numeric args when relevant:\n"
        "- set_brightness: args {\"level\":0..100}\n"
        "- set_font_size: args {\"size\":10..60}\n"
        "If the user asks a general question, set type=query and intent=\"ask\".\n"
        "If unsure, set type=display_only.\n"
        "Return JSON only."
    )
    prompt = f"{system}\nUser text: {text}\nJSON:"
    raw = call_ollama(prompt, timeout_s=20)

    obj = extract_json(raw)
    if not obj:
        return {"type": "display_only", "intent": "display", "args": {}, "response_text": ""}

    t = obj.get("type", "display_only")
    intent = obj.get("intent", "display")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        args = {}

    # Clamp unsafe/unknown commands
    if t == "command" and intent not in ALLOWED_COMMANDS:
        return {"type": "display_only", "intent": "display", "args": {}, "response_text": ""}

    obj["type"] = t
    obj["intent"] = intent
    obj["args"] = args
    obj.setdefault("response_text", "")
    return obj

def llm_answer(question: str) -> str:
    prompt = (
        "Answer in 1-4 sentences. Be direct.\n"
        f"Question: {question}\nAnswer:"
    )
    return call_ollama(prompt, timeout_s=35)

def execute_command(intent: str, args: dict) -> str:
    if intent == "help":
        return "Commands: help, clear screen, set brightness <0-100>, set font size <10-60>."
    if intent == "clear_screen":
        print("\n" + ("-" * 70) + "\n")
        return "Cleared."
    if intent == "set_brightness":
        level = int(args.get("level", 50))
        level = max(0, min(100, level))
        # Stub: integrate with your display/OS later
        return f"(Stub) Brightness set to {level}."
    if intent == "set_font_size":
        size = int(args.get("size", 18))
        size = max(10, min(60, size))
        # Stub: integrate with your UI later
        return f"(Stub) Font size set to {size}."
    return "Unknown command."

def detect_lang_limited(model, audio_f32):
    if hasattr(model, "detect_language"):
        try:
            lang, prob = model.detect_language(audio_f32)
            if lang in ALLOWED_LANGS:
                return lang, prob
        except Exception:
            pass

    best_lang = "en"
    best_score = -1
    for lang in ALLOWED_LANGS:
        segs, _ = model.transcribe(audio_f32, language=lang, task="transcribe", vad_filter=True)
        txt = "".join(s.text for s in segs).strip()
        score = len(txt)
        if score > best_score:
            best_score = score
            best_lang = lang
    return best_lang, None

def main():
    print("Loading Whisper model...")
    model = WhisperModel("small", device="cpu", compute_type="int8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(2.0)

    buf = np.zeros((0,), dtype=np.int16)
    target_samples = int(SAMPLE_RATE * CHUNK_SEC)

    print(f"Listening UDP on :{PORT}. Speak (EN/HI/TR). Output will be English.")
    print("Tip: say 'assistant' + your command/question to route reliably.\n")

    while True:
        data, addr = sock.recvfrom(4096)

        # validate packet
        if len(data) != 4 + 2 + EXPECTED_PAYLOAD:
            continue
        if data[:4] != MAGIC:
            continue
        payload_len = struct.unpack("<H", data[4:6])[0]
        if payload_len != EXPECTED_PAYLOAD:
            continue

        samples = np.frombuffer(data[6:], dtype=np.int16)
        buf = np.concatenate([buf, samples])

        if len(buf) >= target_samples:
            chunk_i16 = buf[:target_samples]
            buf = buf[target_samples:]

            audio = chunk_i16.astype(np.float32) / 32768.0

            # detect source language (limited set)
            src_lang, conf = detect_lang_limited(model, audio)
            src_label = LANG_NAME.get(src_lang, src_lang)
            conf_str = f"{conf:.2f}" if isinstance(conf, float) else "N/A"

            # ALWAYS output English via task="translate"
            segments, _info = model.transcribe(
                audio,
                language=src_lang,
                task="translate",
                vad_filter=True
            )

            out_en = "".join(s.text for s in segments).strip()
            if not out_en:
                continue

            print(f"\nFROM: {addr}  SRC: {src_label} ({src_lang})  CONF: {conf_str}")
            print("EN :", out_en)

            # ---- NEW: LLM pipeline ----
            if should_route(out_en):
                route = llm_route(out_en)

                if route["type"] == "command":
                    print("ROUTE:", route)
                    result = execute_command(route["intent"], route["args"])
                    print("CMD_RESULT:", result)

                elif route["type"] == "query":
                    print("ROUTE:", route)
                    answer = llm_answer(out_en)
                    print("A :", answer)

if __name__ == "__main__":
    main()