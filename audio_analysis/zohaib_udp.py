import socket, struct
import numpy as np
from faster_whisper import WhisperModel

# ---- UDP framing ----
MAGIC = b"AUD0"
PORT = 5005
SAMPLES_PER_PACKET = 256
EXPECTED_PAYLOAD = SAMPLES_PER_PACKET * 2  # 512 bytes

# ---- Audio ----
SAMPLE_RATE = 16000
CHUNK_SEC = 3.0

ALLOWED_LANGS = ["en", "hi", "tr"]
LANG_NAME = {"en": "English", "hi": "Hindi", "tr": "Turkish"}

def detect_lang_limited(model, audio_f32):
    """
    Detect language but limit to en/hi/tr.
    Uses model.detect_language() if available; otherwise falls back to a forced
    decode heuristic to choose among allowed languages.
    Returns (lang_code, conf_or_None).
    """
    if hasattr(model, "detect_language"):
        try:
            lang, prob = model.detect_language(audio_f32)
            if lang in ALLOWED_LANGS:
                return lang, prob
        except Exception:
            pass

    # Fallback: choose best among allowed by trying short forced transcribes.
    # We keep it lightweight by using task="transcribe" for scoring only.
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

    print(f"Listening UDP on :{PORT}. Speak (EN/HI/TR). Output will be English. Ctrl+C to stop.")

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
                language=src_lang,   # constrain to detected allowed language
                task="translate",    # force English output
                vad_filter=True
            )

            out_en = "".join(s.text for s in segments).strip()
            if out_en:
                print(f"\nFROM: {addr}  SRC: {src_label} ({src_lang})  CONF: {conf_str}")
                print("EN :", out_en)

if __name__ == "__main__":
    main()
