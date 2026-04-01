import numpy as np
import sounddevice as sd
import json, re, requests
from faster_whisper import WhisperModel

# -------- Settings --------
SAMPLE_RATE = 16000
CHUNK_SEC = 2.0          # mic testing; later we can go 1.0
OLLAMA_MODEL = "llama3.2:1b"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# Wake-word routing to avoid random speech triggering
REQUIRE_WAKE_WORD = False #can set true to require assistant before each action 
WAKE_WORDS = ("assistant", "hey assistant", "computer", "jarvis")

# Only allow these commands (safe set)
ALLOWED_COMMANDS = {"help", "clear_screen", "set_brightness", "set_font_size"}

# -------- Ollama --------
def call_ollama(prompt: str, timeout_s: int = 30) -> str:
    r = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=timeout_s
    )
    r.raise_for_status()
    return r.json()["response"].strip()

def extract_json(raw: str):
    # grab first JSON object found
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def normalize_route(obj, original_text: str):
    """
    Make router output safe + schema-complete.
    Always returns dict with keys: type, intent, args.
    """
    if not isinstance(obj, dict):
        return {"type": "display_only", "intent": "display", "args": {}}

    t = obj.get("type")
    intent = obj.get("intent", "display")
    args = obj.get("args", {})

    if not isinstance(args, dict):
        args = {}

    # If model forgot "type", infer it:
    if t not in ("command", "query", "display_only", "ignore"):
        # simple inference: question mark => query else display_only
        if original_text.strip().endswith("?"):
            t = "query"
        else:
            t = "display_only"

    # Clamp commands
    if t == "command" and intent not in ALLOWED_COMMANDS:
        t = "display_only"
        intent = "display"
        args = {}

    return {"type": t, "intent": intent, "args": args}

def route(text: str):
    schema = (
        "Return ONLY JSON matching exactly this schema:\n"
        "{\"type\":\"command|query|display_only|ignore\","
        "\"intent\":\"string\",\"args\":{}}\n"
        "- If it's a question, type MUST be \"query\" and intent MUST be \"ask\".\n"
        "- If it's a command, type MUST be \"command\" and intent MUST be one of: "
        + ", ".join(sorted(ALLOWED_COMMANDS)) + ".\n"
        "- Otherwise type MUST be \"display_only\".\n"
        "- args MUST be an object ({} if none).\n"
        "No extra keys. No commentary. JSON only."
    )

    # Keep it short for 1B model reliability
    prompt = f"{schema}\nText: {text}\nJSON:"

    raw = call_ollama(prompt, timeout_s=20)
    obj = extract_json(raw)
    return normalize_route(obj, text)

def answer(text: str):
    return call_ollama(
        "Answer in 1-3 sentences, direct and helpful.\n"
        f"Question: {text}\nAnswer:",
        timeout_s=30
    )

def has_wake_word(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in WAKE_WORDS)

# -------- Main --------
print("Loading Whisper...")
model = WhisperModel("small", device="cpu", compute_type="int8")

print("Speak now... (Ctrl+C to stop)")
if REQUIRE_WAKE_WORD:
    print("Routing requires wake word (say 'assistant ...').\n")
else:
    print("Routing does NOT require wake word.\n")

while True:
    audio = sd.rec(
        int(SAMPLE_RATE * CHUNK_SEC),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32"
    )
    sd.wait()

    audio = audio.flatten()

    segments, _ = model.transcribe(
        audio,
        language="en",
        task="transcribe",
        vad_filter=True
    )

    text = "".join(s.text for s in segments).strip()
    if not text:
        continue

    print("\nYou said:", text)

    # Gate routing to wake word to avoid random chatter
    if REQUIRE_WAKE_WORD and not has_wake_word(text):
        continue

    r = route(text)
    print("Route:", r)

    if r["type"] == "query":
        a = answer(text)
        print("Answer:", a)
    elif r["type"] == "command":
        print("Command detected (stub):", r["intent"], r["args"])