"""
Real-time speaker recognition + transcription
-------------------------------------------------
This script does two things:
1) ENROLL: Capture short voice samples for each speaker name you provide and build a
   voiceprint database (speaker embeddings) using SpeechBrain ECAPA model.
2) RUN: Listen to the microphone in real time, detect speech with WebRTC VAD, transcribe
   with Faster-Whisper, identify the closest enrolled speaker for each segment, and print
   subtitle-like lines, e.g.

   Johnny: "Hi, my name is Johnny"
   Kate: "Hi, my name is Kate"

It also optionally writes a live transcript file (transcript.txt).

USAGE (examples):
-----------------
# 1) Install dependencies (suggest a fresh venv)
#    Python 3.9–3.11 recommended
#    On Linux: you may need `portaudio` dev libs (e.g., `sudo apt-get install portaudio19-dev`)

#    pip install --upgrade pip
#    pip install sounddevice numpy webrtcvad faster-whisper speechbrain torchaudio pydub

# 2) Enroll speakers (record ~8 seconds per person)
#    python realtime_transcribe_diarize.py --mode enroll --names "Johnny,Kate" --seconds 8

# 3) Run the live system (use a small faster-whisper model for speed)
#    python realtime_transcribe_diarize.py --mode run --whisper-model small --device cpu

NOTES:
- This performs *speaker identification* (assigns an existing enrolled identity) rather than blind
  diarization (discovering unknown speakers). If an unknown person speaks, they'll be labeled as the
  closest enrolled voice; set a similarity threshold to print "Unknown" when not close enough.
- On first run, faster-whisper will download the model. Choose `tiny`, `base`, `small`, `medium`, or `large-v3`.
- For best results: use a decent mic, keep background noise moderate, and provide at least 6–10s of
  enrollment audio per speaker.
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import sounddevice as sd
import webrtcvad
import wave
import io

# SpeechBrain speaker embedding model
from speechbrain.pretrained import EncoderClassifier
# Faster-Whisper ASR
from faster_whisper import WhisperModel

# -----------------------------
# Config & Helpers
# -----------------------------
SAMPLE_RATE = 16000  # 16kHz mono for VAD + ASR
FRAME_MS = 20        # VAD frame size (10, 20, or 30ms only) - smaller frame reduces latency
VAD_AGGRESSIVENESS = 2  # 0-3 (3 = most aggressive)
SPEAKER_DB = "speakers.json"  # stores speaker names + embeddings
TRANSCRIPT_OUT = "transcript.txt"

@dataclass
class AudioChunk:
    pcm: bytes  # 16-bit mono little-endian
    timestamp: float


def int16_pcm_from_float32(block: np.ndarray) -> bytes:
    block = np.clip(block, -1.0, 1.0)
    ints = (block * 32767).astype(np.int16)
    return ints.tobytes()


def float32_from_int16(pcm: bytes) -> np.ndarray:
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32767.0
    return arr


def write_wav_bytes_to_file(pcm: bytes, sample_rate: int, path: str) -> None:
    # Write raw PCM (int16 mono) to a WAV file using built-in wave module
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-9)
    b = b / (np.linalg.norm(b) + 1e-9)
    return float(np.dot(a, b))


def normalize_name(name: str) -> str:
    # persistent normalization: strip, collapse spaces, and lowercase
    return " ".join(name.strip().split()).lower()


# -----------------------------
# Microphone Stream
# -----------------------------
class MicStream:
    def __init__(self, sample_rate=SAMPLE_RATE, block_ms=30):
        self.sample_rate = sample_rate
        self.block_size = int(sample_rate * block_ms / 1000)
        self.q: "queue.Queue[AudioChunk]" = queue.Queue()
        self.stream = None
        self._running = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            # Print stream underflow/overflow warnings
            print(status, file=sys.stderr)
        pcm = int16_pcm_from_float32(indata[:, 0])  # mono
        self.q.put(AudioChunk(pcm=pcm, timestamp=time.time()))

    def start(self):
        self._running = True
        self.stream = sd.InputStream(
            channels=1,
            samplerate=self.sample_rate,
            dtype="float32",
            blocksize=self.block_size,
            callback=self._callback,
        )
        self.stream.start()

    def stop(self):
        self._running = False
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()

    def read(self, timeout=1.0) -> AudioChunk:
        return self.q.get(timeout=timeout)


# -----------------------------
# VAD Segmenter
# -----------------------------
class VADSegmenter:
    def __init__(self, sample_rate=SAMPLE_RATE, frame_ms=FRAME_MS, padding_ms=150):
        # reduce default padding_ms to shorten trailing-silence latency
        self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self.sample_rate = sample_rate
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2  # int16 mono
        self.padding_frames = int(padding_ms / frame_ms)
        self.speech_frames: List[bytes] = []
        self.trailing_non_speech = 0
        self.in_speech = False

    def process(self, chunk: AudioChunk) -> List[bytes]:
        """
        Feed one AudioChunk (which may contain multiple frames) and
        return a list of *complete* speech segments (as raw PCM bytes).
        """
        segments = []
        buf = chunk.pcm
        # Split into VAD-sized frames
        for start in range(0, len(buf), self.frame_bytes):
            frame = buf[start:start + self.frame_bytes]
            if len(frame) < self.frame_bytes:
                # buffer until next chunk
                break
            is_speech = self.vad.is_speech(frame, self.sample_rate)
            if is_speech:
                self.speech_frames.append(frame)
                self.trailing_non_speech = 0
                self.in_speech = True
            else:
                if self.in_speech:
                    self.trailing_non_speech += 1
                    if self.trailing_non_speech > self.padding_frames:
                        # finalize segment
                        segments.append(b"".join(self.speech_frames))
                        self.speech_frames.clear()
                        self.trailing_non_speech = 0
                        self.in_speech = False
                # else: remain idle
        return segments

    def flush(self) -> List[bytes]:
        if self.speech_frames:
            seg = [b"".join(self.speech_frames)]
            self.speech_frames.clear()
            self.trailing_non_speech = 0
            self.in_speech = False
            return seg
        return []


# -----------------------------
# Speaker Embeddings via SpeechBrain
# -----------------------------
class SpeakerEncoder:
    def __init__(self, device: str = "cpu"):
        # Downloads/loads ECAPA model
        self.classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            run_opts={"device": device},
            # don't set savedir to avoid creating symlinks on Windows; use HF cache instead
        )

    def embed_wav_pcm(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        # Convert raw PCM (int16) bytes to float32 numpy array
        import torch
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        wav = torch.tensor(samples).unsqueeze(0)  # [1, T]
        with torch.no_grad():
            emb = self.classifier.encode_batch(wav)
        # emb shape [1,1,192]; squeeze to 192
        vec = emb.squeeze().cpu().numpy()
        # return L2-normalized embedding for stable cosine comparisons
        vec = vec / (np.linalg.norm(vec) + 1e-9)
        return vec


# -----------------------------
# Enrollment DB
# -----------------------------
class SpeakerDB:
    def __init__(self, path=SPEAKER_DB):
        self.path = path
        self.db: Dict[str, List[float]] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.db = json.load(f)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.db, f, indent=2)

    def add(self, name: str, emb: np.ndarray):
        key = normalize_name(name)
        # store embeddings as a list of vectors per speaker to allow averaging
        existing = self.db.get(key)
        if existing is None:
            # first entry: store as list of embeddings
            self.db[key] = [emb.tolist()]
        else:
            # existing may be a single vector (legacy) or list of vectors
            if isinstance(existing[0], list) or isinstance(existing[0], float) or isinstance(existing[0], int):
                # if existing is a flat vector (legacy), wrap it
                if not isinstance(existing[0], list):
                    existing = [existing]
            existing.append(emb.tolist())
            self.db[key] = existing
        self.save()

    def replace(self, name: str, emb: np.ndarray):
        """Replace all embeddings for a speaker with a single embedding (used for re-recording reference)."""
        key = normalize_name(name)
        self.db[key] = [emb.tolist()]
        self.save()

    def names(self) -> List[str]:
        return list(self.db.keys())

    def get_matrix(self) -> Tuple[List[str], np.ndarray]:
        names = []
        embs = []
        for k, v in self.db.items():
            names.append(k)
            # v may be a list of embeddings or a single embedding
            if isinstance(v, list) and len(v) and isinstance(v[0], list):
                arrs = [np.array(x, dtype=np.float32) for x in v]
                mean_emb = np.mean(np.stack(arrs, axis=0), axis=0)
                embs.append(mean_emb)
            else:
                # legacy single vector
                embs.append(np.array(v, dtype=np.float32))
        if not embs:
            return names, np.zeros((0, 192), dtype=np.float32)
        mat = np.stack(embs, axis=0)
        # ensure embeddings are normalized (safety)
        norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        mat = mat / norms
        return names, mat


# -----------------------------
# Transcriber (Faster-Whisper)
# -----------------------------
class Transcriber:
    def __init__(self, model_size: str = "small", device: str = "cpu"):
        self.model = WhisperModel(model_size, device=device, compute_type="int8" if device == "cpu" else "float16")

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        # Convert int16 PCM bytes to float32 numpy array in range [-1,1]
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # faster-whisper accepts numpy arrays; it assumes the audio is at the model/sample rate.
        segments, _info = self.model.transcribe(arr, vad_filter=False)
        text = " ".join([s.text.strip() for s in segments]).strip()
        return text


# -----------------------------
# Core Pipeline
# -----------------------------
class LiveRecognizer:
    def __init__(self, whisper_model: str, device: str, unknown_threshold: float = 0.59,
                 min_segment_s: float = 0.35, min_rms: float = 0.01,
                 min_words: int = 2, min_alpha_ratio: float = 0.5):
        # Filters to reduce spurious ASR / embeddings
        self.mic = MicStream(sample_rate=SAMPLE_RATE, block_ms=FRAME_MS)
        self.vad = VADSegmenter(sample_rate=SAMPLE_RATE, frame_ms=FRAME_MS, padding_ms=300)
        self.spk_encoder = SpeakerEncoder(device=device)
        self.db = SpeakerDB(SPEAKER_DB)
        self.transcriber = Transcriber(model_size=whisper_model, device=device)
        self.unknown_threshold = unknown_threshold
        self.min_segment_s = min_segment_s
        self.min_rms = min_rms
        self.min_words = min_words
        self.min_alpha_ratio = min_alpha_ratio
        self._stop = False
        self._writer_lock = threading.Lock()
        # prepare transcript file
        with open(TRANSCRIPT_OUT, "w", encoding="utf-8") as f:
            f.write("")

    def closest_speaker(self, emb: np.ndarray) -> Tuple[str, float]:
        # Compute similarity against each stored embedding for each speaker and return
        # the single best match (helps when some stored embeddings are better representatives).
        if not self.db.db:
            return ("Unknown", 0.0)
        best_name = "Unknown"
        best_score = 0.0
        emb_n = emb / (np.linalg.norm(emb) + 1e-9)
        for name, stored in self.db.db.items():
            # stored may be a list of embeddings or a single embedding
            if isinstance(stored, list) and stored and isinstance(stored[0], list):
                for v in stored:
                    v_arr = np.array(v, dtype=np.float32)
                    v_n = v_arr / (np.linalg.norm(v_arr) + 1e-9)
                    score = float(np.dot(v_n, emb_n))
                    if score > best_score:
                        best_score = score
                        best_name = name
            else:
                v_arr = np.array(stored, dtype=np.float32)
                v_n = v_arr / (np.linalg.norm(v_arr) + 1e-9)
                score = float(np.dot(v_n, emb_n))
                if score > best_score:
                    best_score = score
                    best_name = name
        return best_name, best_score

    def append_transcript(self, line: str):
        with self._writer_lock:
            print(line)
            with open(TRANSCRIPT_OUT, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def run(self):
        if not self.db.names():
            print("No enrolled speakers found. Run in --mode enroll first.")
            return
        print(f"Loaded speakers: {', '.join(self.db.names())}")
        print("Listening... Press Ctrl+C to stop.\n")
        self.mic.start()
        try:
            while not self._stop:
                try:
                    chunk = self.mic.read(timeout=0.5)
                except queue.Empty:
                    continue
                segments = self.vad.process(chunk)
                for pcm in segments:
                    # pre-filters: duration and RMS
                    duration_s = len(pcm) / (2 * SAMPLE_RATE)
                    if duration_s < self.min_segment_s:
                        continue
                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
                    if rms < self.min_rms:
                        continue

                    # Transcribe
                    text = self.transcriber.transcribe_pcm(pcm, SAMPLE_RATE)
                    if not text:
                        continue

                    # text-level simple heuristics: minimum words and alphabetic ratio
                    words = [w for w in text.split() if w.strip()]
                    if len(words) < self.min_words:
                        continue
                    alpha_chars = sum(1 for c in text if c.isalpha())
                    alpha_ratio = alpha_chars / max(1, len(text))
                    if alpha_ratio < self.min_alpha_ratio:
                        continue

                    # Speaker ID
                    emb = self.spk_encoder.embed_wav_pcm(pcm, SAMPLE_RATE)
                    name, score = self.closest_speaker(emb)
                    label = name if score >= self.unknown_threshold else "Unknown"
                    if label == "Unknown":
                        line = f"{label}: \"{text}\" (dur={duration_s:.2f}s rms={rms:.3f} sim={score:.3f})"
                    else:
                        line = f"{label}: \"{text}\" (sim={score:.3f})"
                    self.append_transcript(line)
        except KeyboardInterrupt:
            pass
        finally:
            # flush any remaining
            for pcm in self.vad.flush():
                duration_s = len(pcm) / (2 * SAMPLE_RATE)
                if duration_s < self.min_segment_s:
                    continue
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
                if rms < self.min_rms:
                    continue
                text = self.transcriber.transcribe_pcm(pcm, SAMPLE_RATE)
                if not text:
                    continue
                words = [w for w in text.split() if w.strip()]
                if len(words) < self.min_words:
                    continue
                alpha_chars = sum(1 for c in text if c.isalpha())
                alpha_ratio = alpha_chars / max(1, len(text))
                if alpha_ratio < self.min_alpha_ratio:
                    continue
                emb = self.spk_encoder.embed_wav_pcm(pcm, SAMPLE_RATE)
                name, score = self.closest_speaker(emb)
                label = name if score >= self.unknown_threshold else "Unknown"
                if label == "Unknown":
                    line = f"{label}: \"{text}\" (dur={duration_s:.2f}s rms={rms:.3f} sim={score:.3f})"
                else:
                    line = f"{label}: \"{text}\" (sim={score:.3f})"
                self.append_transcript(line)
            self.mic.stop()
            print("Stopped.")


# -----------------------------
# Enrollment Routine
# -----------------------------

def calibrate(name: str, phrases: List[str], seconds: int = 4, device: str = "cpu", repeats: int = 1):
    """Record several short phrases to append multiple embeddings for a speaker.
    This helps create a robust averaged embedding for better matching.
    """
    print(f"Calibration starting for '{name}'. We'll record {len(phrases)} short phrases.")
    print("Speak naturally; avoid background noise. Press Enter to start each phrase.")
    encoder = SpeakerEncoder(device=device)
    db = SpeakerDB(SPEAKER_DB)

    # interactive calibration with accept/retry when noisy or low-similarity
    for r in range(repeats):
        print(f"--- Repeat {r+1}/{repeats} ---")
        for idx, phrase in enumerate(phrases, start=1):
            attempt = 0
            accepted = False
            while attempt <= getattr(calibrate, 'max_retries', 2) and not accepted:
                prompt = f"[{idx}/{len(phrases)}] Press Enter then say: '{phrase}' (attempt {attempt+1})"
                input(prompt)
                # compute current centroid for this speaker (if present)
                key = normalize_name(name)
                names, mat = db.get_matrix()
                centroid = None
                if key in names:
                    i = names.index(key)
                    centroid = mat[i]

                mic = sd.InputStream(channels=1, samplerate=SAMPLE_RATE, dtype="float32")
                mic.start()
                buf = []
                start = time.time()
                while time.time() - start < seconds:
                    data, _ = mic.read(int(SAMPLE_RATE * 0.2))  # 200ms chunks
                    buf.append(int16_pcm_from_float32(data[:, 0]))
                mic.stop()
                pcm = b"".join(buf)

                # compute RMS to give user feedback about noise/level
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0

                emb = encoder.embed_wav_pcm(pcm, SAMPLE_RATE)
                # similarity before adding (if centroid exists)
                sim_before = None
                if centroid is not None:
                    sim_before = float(np.dot(centroid, emb) / (np.linalg.norm(centroid) * np.linalg.norm(emb) + 1e-9))
                    print(f"Similarity to current centroid: {sim_before*100:.1f}% | RMS={rms:.4f}")
                else:
                    print(f"No existing centroid for this speaker (first sample). RMS={rms:.4f}")

                # decision: accept or retry
                # If sim_before is present and below threshold, prompt to retry
                thresh = getattr(calibrate, 'accept_threshold', 0.55)
                if sim_before is not None and sim_before < thresh:
                    remaining = getattr(calibrate, 'max_retries', 2) - attempt
                    print(f"Low similarity ({sim_before*100:.1f}%) — you may want to retry. Remaining retries: {remaining}")
                    choice = input("Press Enter to retry, type 'k' to keep this sample, 'r' to re-record reference, or 's' to skip adding: ").strip().lower()
                    if choice == 's':
                        print('Skipping this sample.')
                        accepted = False
                        break
                    if choice == 'r':
                        # re-record reference: capture a longer enrollment and replace current DB entry
                        print('Re-recording reference enrollment for', name)
                        mic2 = sd.InputStream(channels=1, samplerate=SAMPLE_RATE, dtype="float32")
                        mic2.start()
                        buf2 = []
                        start2 = time.time()
                        enroll_seconds = max(6, seconds)
                        print(f'Recording {enroll_seconds}s for reference...')
                        while time.time() - start2 < enroll_seconds:
                            data2, _ = mic2.read(int(SAMPLE_RATE * 0.2))
                            buf2.append(int16_pcm_from_float32(data2[:, 0]))
                        mic2.stop()
                        pcm2 = b"".join(buf2)
                        emb2 = encoder.embed_wav_pcm(pcm2, SAMPLE_RATE)
                        db.replace(name, emb2)
                        out_wav2 = f"enroll_{normalize_name(name)}.wav"
                        write_wav_bytes_to_file(pcm2, SAMPLE_RATE, out_wav2)
                        print(f'Replaced reference and saved {out_wav2}.')
                        # after replacing reference, recompute centroid for next loop
                        names, mat = db.get_matrix()
                        if key in names:
                            i = names.index(key)
                            centroid = mat[i]
                        # re-evaluate similarity of current sample to new centroid
                        if centroid is not None:
                            sim_before = float(np.dot(centroid, emb) / (np.linalg.norm(centroid) * np.linalg.norm(emb) + 1e-9))
                            print(f"New similarity to updated centroid: {sim_before*100:.1f}%")
                        # now accept or continue based on new sim
                        if sim_before is not None and sim_before >= thresh:
                            accepted = True
                            break
                        else:
                            attempt += 1
                            continue
                    if choice == 'k':
                        accepted = True
                    else:
                        # retry
                        attempt += 1
                        continue
                else:
                    # Accept by default
                    accepted = True

            # finished attempts
            if accepted:
                db.add(name, emb)
                # recompute centroid and show new similarity
                names, mat = db.get_matrix()
                if key in names:
                    i = names.index(key)
                    new_centroid = mat[i]
                    sim_after = float(np.dot(new_centroid, emb) / (np.linalg.norm(new_centroid) * np.linalg.norm(emb) + 1e-9))
                    print(f"Similarity after appending (to new centroid): {sim_after*100:.1f}%")
                out_wav = f"calibrate_{normalize_name(name)}_{r+1}_{idx}.wav"
                write_wav_bytes_to_file(pcm, SAMPLE_RATE, out_wav)
                print(f"Saved {out_wav} and appended embedding for '{name}'.\n")
            else:
                print(f"No embedding saved for phrase {idx} (attempts exhausted or skipped).\n")

    print(f"Calibration complete. Updated {SPEAKER_DB}.")


def enroll(names: List[str], seconds: int = 8, device: str = "cpu"):
    print("Enrollment starting. We'll record each person for ~%d seconds." % seconds)
    print("Speak naturally; avoid background noise.\n")
    encoder = SpeakerEncoder(device=device)
    db = SpeakerDB(SPEAKER_DB)

    for name in names:
        input(f"Press Enter to start recording for '{name}'. Then speak...")
        mic = sd.InputStream(channels=1, samplerate=SAMPLE_RATE, dtype="float32")
        mic.start()
        buf = []
        start = time.time()
        while time.time() - start < seconds:
            data, _ = mic.read(int(SAMPLE_RATE * 0.2))  # 200ms chunks
            buf.append(int16_pcm_from_float32(data[:, 0]))
        mic.stop()
        pcm = b"".join(buf)
        # Optional: save raw enrollment wav
        out_wav = f"enroll_{name}.wav"
        write_wav_bytes_to_file(pcm, SAMPLE_RATE, out_wav)
        # compute duration from pcm bytes (int16 mono)
        duration_s = len(pcm) / (2 * SAMPLE_RATE)
        print(f"Saved {out_wav} ({duration_s:.2f}s)")

        emb = encoder.embed_wav_pcm(pcm, SAMPLE_RATE)
        db.add(name, emb)
        print(f"Enrolled '{name}'.\n")

    print(f"Enrollment complete. Stored in {SPEAKER_DB}.")


# -----------------------------
# Main
# -----------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="Real-time speaker-ID captioner")
    ap.add_argument("--mode", choices=["enroll", "run", "calibrate"], required=True)
    ap.add_argument("--names", type=str, default="", help="Comma-separated names to enroll")
    ap.add_argument("--seconds", type=int, default=8, help="Seconds per speaker during enrollment")
    ap.add_argument("--phrases", type=str, default="", help="Comma-separated phrases to speak during calibrate mode")
    ap.add_argument("--repeats", type=int, default=1, help="How many times to repeat the set of calibration phrases")
    ap.add_argument("--calib-threshold", type=float, default=0.55, help="Minimum similarity to existing centroid to accept a new calibration sample")
    ap.add_argument("--calib-retries", type=int, default=2, help="How many retries to allow for a calibration phrase if similarity is low")
    ap.add_argument("--mode-info", dest="mode_info", action="store_true", help="Show speaker DB info and exit")
    ap.add_argument("--whisper-model", type=str, default="small", help="Whisper model size (tiny/base/small/medium/large-v3)")
    ap.add_argument("--device", type=str, default="cpu", help="cpu or cuda")
    ap.add_argument("--unknown-threshold", type=float, default=0.59, help="Cosine similarity threshold for Unknown label (0-1)")
    ap.add_argument("--min-segment-s", type=float, default=0.35, help="Minimum segment duration (s) to consider")
    ap.add_argument("--min-rms", type=float, default=0.01, help="Minimum RMS energy required for a segment")
    ap.add_argument("--min-words", type=int, default=2, help="Minimum number of words in ASR output to accept")
    ap.add_argument("--min-alpha-ratio", type=float, default=0.5, help="Minimum alphabetic character ratio in ASR output")
    return ap.parse_args()

def main():
    args = parse_args()

    if args.mode_info:
        db = SpeakerDB(SPEAKER_DB)
        print("Speakers in DB:")
        for k, v in db.db.items():
            count = len(v) if isinstance(v, list) and v and isinstance(v[0], list) else 1
            print(f"- {k}: {count} embedding(s)")
        return

    if args.mode == "enroll":
        if not args.names:
            print('--names required for enroll mode (e.g., --names "Johnny,Kate")')
            sys.exit(1)
        names = [n.strip() for n in args.names.split(",") if n.strip()]
        enroll(names, seconds=args.seconds, device=args.device)
        return

    elif args.mode == "calibrate":
        if not args.names:
            print('--names required for calibrate mode (e.g., --names "Zohaib")')
            sys.exit(1)
        name = args.names.split(",")[0].strip()
        phrases = [p.strip() for p in args.phrases.split(",") if p.strip()] if args.phrases else [
            f"Hello, my name is {name}.",
            "Testing one two three.",
            "Can you hear me clearly?",
            "Please say this sentence to help calibration.",
        ]
        calibrate.accept_threshold = args.calib_threshold
        calibrate.max_retries = args.calib_retries
        calibrate(name, phrases, seconds=args.seconds, device=args.device, repeats=args.repeats)
        return

    elif args.mode == "run":
        rec = LiveRecognizer(
            whisper_model=args.whisper_model,
            device=args.device,
            unknown_threshold=args.unknown_threshold,
            min_segment_s=args.min_segment_s,
            min_rms=args.min_rms,
            min_words=args.min_words,
            min_alpha_ratio=args.min_alpha_ratio,
        )
        rec.run()
        return

    print("Unknown mode:", args.mode)
    sys.exit(1)

# def main():
#     args = parse_args()
#     if args.mode == "enroll":
#         if not args.names:
#             print("--names required for enroll mode (e.g., --names \"Johnny,Kate\")")
#             sys.exit(1)
#         names = [n.strip() for n in args.names.split(",") if n.strip()]
#         enroll(names, seconds=args.seconds, device=args.device)
#     elif args.mode == "calibrate":
#         if not args.names:
#             print("--names required for calibrate mode (e.g., --names \"Zohaib\")")
#             sys.exit(1)
#         # use first name only
#         name = args.names.split(",")[0].strip()
#         if args.phrases:
#             phrases = [p.strip() for p in args.phrases.split(",") if p.strip()]
#         else:
#             phrases = [
#                 f"Hello, my name is {name}.",
#                 "Testing one two three.",
#                 "Can you hear me clearly?",
#                 "Please say this sentence to help calibration.",
#                 ]
#     # attach options to the calibrate function for runtime access
#     calibrate.accept_threshold = args.calib_threshold
#     calibrate.max_retries = args.calib_retries
#     calibrate(name, phrases, seconds=args.seconds, device=args.device, repeats=args.repeats)
#     if args.mode_info:
#             db = SpeakerDB(SPEAKER_DB)
#             print("Speakers in DB:")
#             for k, v in db.db.items():
#                 if isinstance(v, list) and len(v) and isinstance(v[0], list):
#                     count = len(v)
#                 else:
#                     count = 1
#                 print(f"- {k}: {count} embedding(s)")
#             return
#     else:
#         rec = LiveRecognizer(
#             whisper_model=args.whisper_model,
#             device=args.device,
#             unknown_threshold=args.unknown_threshold,
#             min_segment_s=args.min_segment_s,
#             min_rms=args.min_rms,
#             min_words=args.min_words,
#             min_alpha_ratio=args.min_alpha_ratio,
#         )
#         rec.run()


if __name__ == "__main__":
    main()
