import numpy as np
import sounddevice as sd
import json
import re
import requests
from faster_whisper import WhisperModel
import screen_brightness_control as sbc

# -------- Settings --------
SAMPLE_RATE = 16000
CHUNK_SEC = 2.0
OLLAMA_MODEL = "llama3.2:1b"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

REQUIRE_WAKE_WORD = False
WAKE_WORDS = ("assistant", "hey assistant", "computer", "jarvis")

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
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def normalize_route(obj, original_text: str):

    if not isinstance(obj, dict):
        return {"type": "display_only", "intent": "display", "args": {}}

    t = obj.get("type")
    intent = obj.get("intent", "display")
    args = obj.get("args", {})

    if not isinstance(args, dict):
        args = {}

    if t not in ("command", "query", "display_only", "ignore"):
        if original_text.strip().endswith("?"):
            t = "query"
        else:
            t = "display_only"

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


def has_wake_word(text: str):

    t = text.lower()

    return any(w in t for w in WAKE_WORDS)


# -------- Brightness Parsing --------

pending_brightness = False

NUM_WORDS = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,
    "six":6,"seven":7,"eight":8,"nine":9,"ten":10,
    "twenty":20,"thirty":30,"forty":40,"fifty":50,
    "sixty":60,"seventy":70,"eighty":80,"ninety":90,
    "hundred":100
}


def parse_brightness(text):

    global pending_brightness

    t = text.lower().strip().replace(".", "")

    # fix whisper mistakes
    t = t.replace("step brightness","set brightness")
    t = t.replace("step the brightness","set brightness")
    t = t.replace("set the brightness","set brightness")
    t = t.replace("brightness set to","set brightness to")

    # full numeric command
    m = re.search(r"(set|change|adjust)\s+(the\s+)?brightness\s+(to\s+)?(\d{1,3})", t)

    if m:
        level = int(m.group(4))
        pending_brightness = False
        return level

    # word numbers
    for word,val in NUM_WORDS.items():
        if f"set brightness to {word}" in t or f"brightness to {word}" in t:
            pending_brightness = False
            return val

    # first half of command
    if "set brightness" in t or t == "brightness":
        pending_brightness = True
        return None

    # second chunk
    if pending_brightness:

        if t.isdigit():
            pending_brightness = False
            return int(t)

        if t in NUM_WORDS:
            pending_brightness = False
            return NUM_WORDS[t]

    return None


def is_brightness_context(text):

    t = text.lower().strip().replace(".", "")

    return (
        "brightness" in t
        or t.isdigit()
        or t in NUM_WORDS
    )


# -------- Main --------

print("Loading Whisper...")

model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)

print("Speak now... (Ctrl+C to stop)")

print("This version uses the LAPTOP microphone.\n")


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

    if REQUIRE_WAKE_WORD and not has_wake_word(text):
        continue


    # ---- brightness command first ----

    level = parse_brightness(text)

    if level is not None:

        level = max(0, min(100, level))

        api_level = 1 if level == 0 else level

        try:

            sbc.set_brightness(api_level)

            current = sbc.get_brightness()

            if isinstance(current, list) and len(current) > 0:
                current = current[0]

            print(f"Brightness set to {current}%")

        except Exception as e:

            print(f"Brightness failed: {e}")

        continue


    # wait for next chunk if brightness context

    if pending_brightness or is_brightness_context(text):

        print("Waiting for brightness value...")

        continue


    # ---- otherwise LLM router ----

    r = route(text)

    print("Route:", r)


    if r["type"] == "query":

        a = answer(text)

        print("Answer:", a)


    elif r["type"] == "command":

        print("Command detected (stub):", r["intent"], r["args"])