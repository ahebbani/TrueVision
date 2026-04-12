# TrueVision — Complete Project Documentation

**Author:** Aditya Hebbani  
**Platform:** Raspberry Pi 4B (or Pi 5) + ESP32  
**Language:** Python 3.10+ (Pi/Mac), C++/Arduino (ESP32)  
**Date:** January 2026

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Hardware Architecture](#2-hardware-architecture)
3. [Software Architecture](#3-software-architecture)
4. [Repository Layout](#4-repository-layout)
5. [Database Schema](#5-database-schema)
6. [ESP32 Firmware (`esp32_firmware/`)](#6-esp32-firmware)
7. [Main Orchestrator (`main.py`)](#7-main-orchestrator)
8. [Facial Recognition (`facial_recognition/`)](#8-facial-recognition)
9. [Audio Analysis (`audio_analysis/`)](#9-audio-analysis)
10. [OLED Output (`oled_output/`)](#10-oled-output)
11. [Data Access Layer (`data_access/`)](#11-data-access-layer)
12. [Summarization Service (`summarization/`)](#12-summarization-service)
13. [Utility & Maintenance Scripts](#13-utility--maintenance-scripts)
14. [Setup & Installation](#14-setup--installation)
15. [Makefile Targets](#15-makefile-targets)
16. [Runtime Flags Reference](#16-runtime-flags-reference)
17. [End-to-End Execution Flow](#17-end-to-end-execution-flow)
18. [Tuning & Configuration](#18-tuning--configuration)
19. [Recreating the Project from Scratch](#19-recreating-the-project-from-scratch)

---

## 1. Project Overview

TrueVision is a privacy-first, fully offline smart-meeting assistant. It runs on a Raspberry Pi connected to a camera and an ESP32 microcontroller that captures audio from an I2S MEMS microphone. When placed in a room, the system:

- **Identifies who is present** using dlib face recognition against a local database.
- **Transcribes conversation** in real time using OpenAI Whisper (via `faster-whisper`) running fully on-device.
- **Generates a one-sentence summary** of each conversation and stores it so the next time that person's face appears, the previous discussion is recalled and displayed.
- **Displays live closed captions** at the bottom of the camera feed, and optionally speaks them aloud via TTS.
- **Mirrors status to a small OLED display** (SSD1306/SH1106, 128×64 pixels, I2C) next to the camera.
- **Stores all data locally** in a single SQLite database. No cloud, no network uploads.

Everything runs on the Pi. The optional summarization service can run locally (via Ollama) or on a separate machine; if it is unavailable, a simpler extractive fallback runs in-process.

---

## 2. Hardware Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        Raspberry Pi 4B                       │
│                                                              │
│  ┌──────────────┐   ┌───────────────┐   ┌────────────────┐  │
│  │ Pi Camera    │   │  faces.db     │   │  OLED SSD1306  │  │
│  │ Module (CSI) │   │  (SQLite)     │   │  128×64  I2C   │  │
│  └──────┬───────┘   └───────────────┘   └────────────────┘  │
│         │                                        │SDA/SCL     │
│  OpenCV / GStreamer / picamera2                              │
│                                                              │
│         UART0 (GPIO 14/15, 921600 baud)                     │
└──────────────────────────────┬───────────────────────────────┘
                               │ TX↔RX
┌──────────────────────────────┴───────────────────────────────┐
│                           ESP32                               │
│                                                              │
│  ┌────────────────┐   ┌──────────┐   ┌────────────────────┐  │
│  │ SPH0645 / INMP │   │ Mode     │   │ Status LEDs        │  │
│  │ 441 MEMS Mic   │   │ Switch   │   │ LED1 (GPIO 9)      │  │
│  │ I2S (GPIO 5,   │   │ (GPIO    │   │ LED2 (GPIO 10)     │  │
│  │  16, 17)       │   │ 22/23 or │   └────────────────────┘  │
│  └────────────────┘   │ 35/36)   │   ┌────────────────────┐  │
│                       └──────────┘   │ Marker Button      │  │
│                                      │ (GPIO 11)          │  │
│                                      └────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Wiring Table

| Signal | ESP32 Pin | Pi Physical Pin | Pi GPIO |
|---|---|---|---|
| UART TX (ESP32 → Pi) | GPIO 1 | Pin 10 | GPIO 15 (RXD) |
| UART RX (Pi → ESP32) | GPIO 3 | Pin 8 | GPIO 14 (TXD) |
| GND | GND | Pin 6 | — |
| I2S BCLK | GPIO 16 | — | — |
| I2S LRCLK/WS | GPIO 17 | — | — |
| I2S Data | GPIO 5 | — | — |
| Mic VDD | 3.3V | — | — |
| OLED SDA | — | Pin 3 | GPIO 2 |
| OLED SCL | — | Pin 5 | GPIO 3 |

---

## 3. Software Architecture

```
main.py
  ├── facial_recognition/camera.py        ← camera open / fallback chain
  ├── facial_recognition/recognizer.py    ← dlib detect + embed + match
  ├── audio_analysis/esp32_serial_audio.py ← UART framing, ring buffer
  ├── audio_analysis/transcription.py     ← Recorder, Transcriber, summaries
  ├── audio_analysis/live_caption.py      ← periodic Whisper, caption update
  ├── audio_analysis/caption_speaker.py   ← TTS (pyttsx3 / espeak fallback)
  ├── oled_output/oled_display.py         ← I2C SSD1306/SH1106 + DummyDisplay
  ├── data_access/db.py                   ← SQLite open, schema, helpers
  └── summarization/remote_client.py      ← HTTP client for FastAPI summarizer

summarization/server.py   ← standalone FastAPI service (needs Ollama)
  └── summarization/ollama_client.py      ← Ollama /api/generate
  └── summarization/prompting.py          ← prompt builder
  └── summarization/text.py               ← clamping + sentence helpers
```

The entire pipeline is single-process on the Pi. No message queues or IPC beyond the single serial port. The ESP32 receiver, heartbeat sender, and Whisper transcription each run in daemon background threads; the main Python thread owns the camera loop and reacts to their results.

---

## 4. Repository Layout

```
TrueVision/
├── main.py                          Top-level orchestrator (entry point)
├── Makefile                         Build/run targets
├── setup_pi.sh                      Pi 4B/5 setup (apt + venv + models)
├── setup_mac.sh                     macOS dev setup (brew + venv + models)
├── probe_uart.py                    Standalone UART probe utility
├── brightness_gui.py                Camera brightness adjustment GUI
│
├── audio_analysis/
│   ├── esp32_serial_audio.py        ESP32 UART packet receiver + ring buffer
│   ├── transcription.py             Whisper wrapper, Recorder, factory
│   ├── live_caption.py              Periodic transcription → caption state
│   ├── caption_speaker.py           TTS speaker (pyttsx3 / espeak)
│   ├── summarize_meetings.py        Offline batch summarization utility
│   └── backfill_transcripts.py      Re-transcribe WAV files into DB
│
├── facial_recognition/
│   ├── camera.py                    Camera open with 3-backend fallback
│   ├── recognizer.py                dlib face detector, embedder, matcher
│   ├── add_face.py                  Interactive CLI to enroll a new person
│   ├── manage_embeddings.py         CLI to audit/prune face templates
│   └── models/
│       └── fetch_models.py          Download dlib model files
│
├── data_access/
│   ├── db.py                        SQLite schema, open_db, helpers
│   └── visualize_db.py              Console + HTML report generator
│
├── oled_output/
│   ├── oled_display.py              luma.oled wrapper + DummyDisplay fallback
│   └── test_oled.py                 Quick OLED hardware test
│
├── summarization/
│   ├── server.py                    FastAPI summarization microservice
│   ├── ollama_client.py             Ollama HTTP client
│   ├── prompting.py                 LLM prompt builder
│   ├── text.py                      Sentence clamping helpers
│   ├── remote_client.py             Pi-side HTTP client to call the service
│   └── backfill_summaries.py        Batch re-summarize existing meetings
│
├── esp32_firmware/
│   ├── truevision_main.ino          Production firmware (full protocol)
│   ├── i2s_uart_audio_streamer.ino  Minimal audio-only streamer
│   ├── sph0645_mic_self_test.ino    Microphone raw-sample self-test
│   ├── test_audio_generator.ino     Tone generator for protocol testing
│   └── uart0_pi_probe_test.ino      Simple UART echo for cable/pin testing
│
├── data/
│   └── recordings/                  WAV files saved during sessions
│
└── docs/
    ├── PROJECT.md                   ← this file
    ├── architecture.md
    └── db_report.html               Generated by `make db-report`
```

---

## 5. Database Schema

Single SQLite file at `data_access/faces.db`. The schema is created automatically on first `open_db()` call and is forward-compatible (no migration needed for new columns).

### `faces` table

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Unique person ID |
| `name` | TEXT | Human-readable name (enrolled via `add_face.py`) |
| `embedding` | BLOB | Legacy 128-float64 dlib embedding (used to seed `face_embeddings`) |
| `created_at` | TEXT | ISO-8601 timestamp of enrollment |
| `last_seen_at` | TEXT | Updated each time that person is recognized |
| `seen_count` | INTEGER | Total recognition events |

### `face_embeddings` table

Stores multiple embedding templates per person (up to `MAX_TEMPLATES_PER_PERSON = 30`).

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | — |
| `face_id` | INTEGER FK | References `faces.id` |
| `embedding` | BLOB | 128 × float64 bytes (1024 bytes). The embedding is a unit-vector in ℝ¹²⁸. |
| `created_at` | TEXT | When this template was captured |
| `quality` | REAL | Laplacian variance of the face crop (blur score); higher = sharper |

The recognizer reads **all** embeddings for all people on each frame and picks the person whose closest template is within `match_threshold` (default 0.6, L2 distance). New templates are added at runtime during recognition if they are sharp enough and sufficiently different from existing ones (diversity check).

### `meetings` table

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | — |
| `person_id` | INTEGER FK | References `faces.id`; `-1` for anonymous audio-only sessions |
| `started_at` | TEXT | ISO-8601 timestamp when recording began |
| `ended_at` | TEXT | When recording stopped (NULL while in progress) |
| `audio_path` | TEXT | Relative path to the `.wav` file in `data/recordings/` |
| `transcript` | TEXT | Full Whisper transcription text (updated mid-session by LiveCaptioner) |
| `summary` | TEXT | One-sentence summary (generated at session end) |

---

## 6. ESP32 Firmware

### `truevision_main.ino` — Production Firmware

This is the primary firmware file. It runs four concurrent FreeRTOS tasks on the ESP32:

1. **Audio Task** — Reads I2S samples, builds framed packets, writes to UART.
2. **UART RX Task** — Receives and dispatches packets from the Pi (heartbeat, ACK, PI_STATUS).
3. **Supervisor Task** — Polls GPIO (mode switch, button), manages LED state machine, detects heartbeat timeouts.
4. **Idle / WDT** — FreeRTOS idle task feeds the watchdog.

#### Serial Protocol

Every message in both directions uses the same frame format:

```
[0xAA] [0x55] [TYPE (1 byte)] [LEN_LO] [LEN_HI] [DATA (LEN bytes)] [CHECKSUM]
```

- **SYNC**: Always `0xAA 0x55`. The Pi receiver scans byte-by-byte until it finds this pair.
- **TYPE**: Identifies the packet kind (see table below).
- **LEN**: 16-bit little-endian count of DATA bytes.
- **CHECKSUM**: `sum(DATA bytes) & 0xFF`. Corrupted packets are silently discarded (counter incremented).

| Direction | Type | Value | Payload |
|---|---|---|---|
| ESP32 → Pi | AUDIO_DATA | `0x01` | Raw int16 PCM, 16 kHz, mono (BUFFER_SIZE = 512 samples = 1024 bytes per packet, 32 ms) |
| ESP32 → Pi | MODE_CHANGE | `0x02` | 1 byte: `0x00`=AUDIO, `0x01`=FACE, `0x02`=BOTH |
| ESP32 → Pi | MARKER | `0x03` | 0 bytes (Pi timestamps on receipt) |
| ESP32 → Pi | DIAG_REQUEST | `0x04` | 0 bytes (requests Pi status) |
| Pi → ESP32 | HEARTBEAT | `0x10` | 1 byte: `0x00` (sent every 3 s) |
| Pi → ESP32 | PI_STATUS | `0x11` | 1+ bytes: error_code + optional ASCII string |
| Pi → ESP32 | ACK | `0x12` | 1 byte: TYPE of the acknowledged packet |

#### I2S Audio Capture

The ESP32 reads 32-bit I2S frames from an SPH0645 or INMP441 MEMS microphone at 16 kHz mono. Because the microphone outputs data in the upper bits of each 32-bit I2S word, each sample is right-shifted by `SAMPLE_SHIFT` (default 14) to produce a signed 16-bit PCM value. These samples are packed into an audio packet and sent immediately via UART. The Pi must consume them at 921600 baud without loss.

#### Board Profiles

The firmware compiles for two board variants. Change `BOARD_PROFILE` before flashing:

| Profile | Value | Mode switch pins | LEDs | Marker button |
|---|---|---|---|---|
| Test board | `1` (default) | GPIO 22, 23 (INPUT_PULLUP) | Disabled | Disabled |
| Production board | `2` | GPIO 35, 36 (INPUT) | Enabled (GPIO 9, 10) | Enabled (GPIO 11) |

#### Mode Switch Logic

A physical two-position switch selects the operating mode. The firmware reads `MODE_PIN_A` and `MODE_PIN_B`:

- Both LOW → `MODE_AUDIO` (stream audio only, Pi skips face detection)
- Both HIGH → `MODE_FACE` (Pi runs face detection only, no audio sent)
- One HIGH, one LOW → `MODE_BOTH` (full system)
- Invalid (both identical and ambiguous) → retain last valid mode; LED2 blinks once every 3 s

When the mode changes, the ESP32 sends a `PKT_MODE_CHANGE` packet. The Pi reacts by stopping/starting sessions accordingly.

#### Button Actions (Production Board, GPIO 11)

| Gesture | Action |
|---|---|
| Short press | Send `PKT_MARKER`; Pi inserts `[MARKER HH:MM:SS]` into active transcript |
| Double short press (within 600 ms) | Force `MODE_BOTH` regardless of switch position |
| Long press (≥ 3 s) | Send `PKT_DIAG_REQUEST`; Pi replies with `PKT_PI_STATUS`; both LEDs flash 3× alternately to confirm ACK |

#### LED Diagnostic Patterns (Production Board)

**LED1 (GPIO 9) — Audio / hardware health:**

| Pattern | Meaning |
|---|---|
| Off | Normal; I2S streaming OK |
| 1 blink every 3 s | Recoverable I2S read errors (still running) |
| Fast blink 4 Hz | I2S driver init failed entirely |
| Solid ON | Mic dead: all-zero samples for > 2 s |

**LED2 (GPIO 10) — Link / Pi health:**

| Pattern | Meaning |
|---|---|
| Off | Normal; Pi heartbeat received within timeout |
| 1 blink every 3 s | Mode switch in invalid state |
| Slow blink 1 Hz | Pi heartbeat timeout (no `0x10` for > 8 s) |
| Fast blink 4 Hz | UART TX overflow (write buffer near full) |
| Solid ON | Pi reported critical error (`PKT_PI_STATUS` with non-zero code) or UART framing error |

**Both LEDs solid for 2 s on boot:** Previous reset was watchdog or panic.  
**Both LEDs alternating 4 Hz:** I2S init failed AND Pi critical error simultaneously.

#### Other Firmware Files

| File | Purpose |
|---|---|
| `i2s_uart_audio_streamer.ino` | Minimal audio-only streamer; no mode switch, no button, no protocol overhead. Good for early bringup testing. |
| `sph0645_mic_self_test.ino` | Reads I2S samples and prints them to Serial for verifying mic wiring. |
| `test_audio_generator.ino` | Sends a synthetic tone packet instead of live audio; used to test the Pi-side packet parser independently of the microphone. |
| `uart0_pi_probe_test.ino` | Simple echo sketch; ESP32 echoes everything it receives on UART0. Used to verify cable continuity and pin assignment. |

---

## 7. Main Orchestrator

**File:** `main.py`  
**Entry point:** `python main.py [flags]`  
**Top-level function:** `recognize_face()`

`main.py` owns the camera loop and wires every subsystem together. Its lifetime is: parse args → open DB → open OLED → open camera → init recognizer → init audio → enter main loop → handle KeyboardInterrupt/SIGTERM cleanup.

### Initialization Sequence

1. **`parse_args()`** — Parses all CLI flags and environment variables (see [section 16](#16-runtime-flags-reference)).
2. **`_log_system_diagnostics()`** — Logs platform, Python version, board model (`/proc/device-tree/model`), GPU throttle state (`vcgencmd get_throttled`), and CMA memory pool size. Runs on every startup to catch thermal/memory issues before they cause silent crashes.
3. **`open_db()`** — Opens `data_access/faces.db`, ensuring all three tables exist. Returns a `sqlite3.Connection`.
4. **OLED init** — Calls `oled_output.oled_display.get_display()`. If luma.oled is absent, hardware is missing, or I2C fails, falls back to `_DummyDisplay` (no-op). Sets `_oled_missing` flag for the ESP32 receiver.
5. **`open_camera()`** — Opens the camera with the configured backend (see [section 8](#8-facial-recognition)).
6. **`Recognizer()`** — Builds the dlib detector, shape predictor, and face recognition model. Will raise `RuntimeError` if the `.dat` model files are absent under `facial_recognition/models/`.
7. **Audio subsystem** — If `--audio` (default), imports `Recorder`, `create_recorder`, `Transcriber`, `summarize_text` from `audio_analysis/transcription`. If the import fails (e.g., faster-whisper not installed), transcription is silently disabled.
8. **ESP32 receiver init** — If `--audio-source` is `esp32-serial` or `auto`, calls `probe_esp32_uart_stream()` to check for sync bytes within a 1-second window. If confirmed (or forced), calls `get_shared_receiver()` to create and start the `ESP32SerialAudioReceiver` with the mode-change, marker, and diagnostic callbacks wired in. This is the only place where ESP32 callbacks are registered.
9. **`Transcriber()`** — Lazily loads the Whisper model. The model runs entirely on CPU (`device="cpu"`, `compute_type="int8"`). The model size is configurable via `--whisper-model` (default `"tiny"`).
10. **`LiveCaptioner()`** and **`CaptionSpeaker()`** — Set up the caption pipeline if transcription is enabled.
11. **OLED "Ready"** — Displays `["Ready", "HH:MM:SS"]` to signal the system is up.

### Main Loop

Each iteration of the `while True:` loop:

1. **Mode transition check** — If the ESP32 sent a `PKT_MODE_CHANGE` (processed in the daemon thread, written to `current_mode[0]`), `_apply_mode_transition()` handles stopping/starting sessions:
   - `MODE_AUDIO`: Stop all face sessions, start one anonymous audio-only session (`AUDIO_SESSION_KEY = -1`).
   - `MODE_FACE`: Stop all sessions, clear presence state.
   - `MODE_BOTH`: Stop the audio-only session if one exists, allow per-person sessions.

2. **`cap.read()`** — Reads one frame from the camera. If this returns `False`, the loop exits.

3. **Marker drain** — Any `PKT_MARKER` events queued by the ESP32 callback are dequeued and appended as `[MARKER HH:MM:SS]` to the transcript column of all active meetings.

4. **Face detection** (skipped in `MODE_AUDIO`):
   - Calls `recog.detect_and_recognize(conn, frame)` which returns a list of `FaceInfo` objects.
   - For each detected face:
     - If known (distance < threshold) and previously absent: fetch previous meeting summary, update `seen_count`/`last_seen_at`, start a recording session, update OLED.
     - If known and already present: maintain presence state.
     - If unknown: draw "Unknown" label; no recording started.
     - **Adaptive template add**: calls `recog.maybe_add_embedding()` to opportunistically add the current embedding if it is high-quality and diverse enough. Prunes templates above `MAX_TEMPLATES_PER_PERSON = 30` after each add.

5. **Bounding box + overlay rendering** — Draws green rectangles, name labels, seen-count, last-seen timestamp, previous-meeting summary (truncated to 48 chars), and a red "REC" indicator on the `display_frame`. The frame is either a copy of the camera image or a black background if `--overlay-only`.

6. **Live captioning** — Calls `captioner.update()` every `--caption-interval` seconds (default 0.7 s). The captioner flushes the current audio file to disk (for ESP32 recorder) and runs Whisper on it, updating the `transcript` column in real time. The latest `--caption-max-words` words are rendered at the bottom of the frame in a black box.

7. **TTS** — If `--speak-captions`, submits the current caption to `CaptionSpeaker.submit()`. The speaker deduplicates, checks the minimum interval (1.2 s default), and enqueues only new words in a daemon thread.

8. **Absence detection** — Any person present in the previous frame who is absent in the current frame gets a grace period (`--absence-grace-sec`, default 2.0 s). After the grace period, `_stop_session()` is called, which: stops the recorder, runs final Whisper transcription on the full WAV file, writes transcript and start time to the meeting row, generates a summary (remote LLM or local extractive fallback), writes summary to DB.

9. **`cv2.imshow()`** — Displays the annotated frame. Press `q` to quit cleanly. Press `t` to toggle transcription on/off at runtime.

### Signal and Exception Handling

- **SIGTERM** — Registered at startup. On receipt, prints `"Received SIGTERM, shutting down..."` and calls `sys.exit(0)`. This allows `systemd` to shut down the service cleanly and ensures `_stop_session()` is invoked for all active sessions.
- **Top-level try/except** in `__main__` — Catches `KeyboardInterrupt` (Ctrl-C) and all other exceptions, prints the error with `traceback`, and exits with code 1. This prevents silent crashes in the service log.

---

## 8. Facial Recognition

### `facial_recognition/camera.py` — Camera Backend

`open_camera(backend, index, width, height, fps)` tries backends in order until one succeeds:

| Backend | Method | Notes |
|---|---|---|
| `opencv` | `cv2.VideoCapture(index)` | Default on most systems |
| `opencv` on macOS | `cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)` | AVFoundation explicit backend |
| `gstreamer` | GStreamer pipeline `libcamerasrc ! ... ! appsink` | Pi Camera via GStreamer; outputs BGR directly |
| `picamera2` | `Picamera2` (Python binding) | Pi Camera Module fallback; delivers `BGR888` array |

Auto mode tries: `opencv` → `gstreamer` → `picamera2`. The GStreamer and Picamera2 backends wrap their capture objects in thin classes with `read()`, `isOpened()`, and `release()` methods matching the OpenCV `VideoCapture` interface so the rest of the system is backend-agnostic.

### `facial_recognition/recognizer.py` — Face Recognizer

**`RecognizerConfig`** dataclass fields:

| Field | Default | Description |
|---|---|---|
| `models_dir` | (required) | Path to directory with `.dat` files |
| `detector_mode` | `'auto'` | `'hog'` (CPU-efficient), `'cnn'` (more accurate), `'auto'` (use CNN if model file present) |
| `match_threshold` | `0.6` | L2 distance cutoff; lower = stricter |
| `quality_min_var` | `120.0` | Minimum Laplacian variance for a face crop to be considered sharp |
| `diversity_min_dist` | `0.20` | New template must be at least this far from all existing templates for the same person |
| `add_cooldown_sec` | `5.0` | Minimum seconds between adding templates for the same person |
| `verbose` | `False` | Print template add/skip reasoning |

**`detect_and_recognize(conn, frame_bgr) → List[FaceInfo]`**:

1. Converts frame to grayscale.
2. Runs `dlib.get_frontal_face_detector()` (HOG) or `dlib.cnn_face_detection_model_v1` (CNN) to find face rectangles.
3. For each rectangle:
   - Runs `dlib.shape_predictor_68_face_landmarks` to find 68 facial landmarks.
   - Runs `dlib.face_recognition_model_v1.compute_face_descriptor()` on the RGB frame to get a 128-float64 embedding vector.
   - Loads all embeddings from `face_embeddings` for all known people (re-queried per frame; no persistent in-memory cache to ensure DB updates are reflected immediately).
   - Computes L2 distance to every stored template. The person with the minimum distance below `match_threshold` is the match.
   - Computes the Laplacian variance of the face crop as a blur/quality score.
4. Returns `FaceInfo(rect, embedding, person_id, name, seen_count, last_seen_at, distance, quality)`.

**`maybe_add_embedding(conn, person_id, emb_live, quality) → bool`**:
- Rejects if quality < `quality_min_var`.
- Rejects if time since last add < `add_cooldown_sec`.
- Rejects if the minimum L2 distance to all existing templates is < `diversity_min_dist`.
- Otherwise inserts new row into `face_embeddings`.

**`update_seen(conn, person_id)`**: Increments `seen_count`, updates `last_seen_at = datetime('now')`.

### `facial_recognition/add_face.py` — Face Enrollment

Interactive script. Opens the camera, waits for a face, captures and stores a 128-float64 embedding in both `faces.embedding` (legacy) and `face_embeddings`. Prompts for the person's name. Can be run as:

```
python -m facial_recognition.add_face
# or
python facial_recognition/add_face.py
```

### `facial_recognition/manage_embeddings.py` — Template Management

CLI tool for auditing and pruning stored face templates.

```
python -m facial_recognition.manage_embeddings --stats
python -m facial_recognition.manage_embeddings --prune 2 --keep 15
python -m facial_recognition.manage_embeddings --delete 3
```

`--prune` keeps the highest-quality templates; worst are removed first (sorted by Laplacian variance, then creation time as tiebreaker).

### `facial_recognition/models/fetch_models.py`

Downloads, bzip2-decompresses, and saves the two required dlib model files to `facial_recognition/models/`:

| File | Size | Source |
|---|---|---|
| `shape_predictor_68_face_landmarks.dat` | ~99 MB | `http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2` |
| `dlib_face_recognition_resnet_model_v1.dat` | ~21 MB | `http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2` |

Optionally downloads `mmod_human_face_detector.dat` for CNN detection.

---

## 9. Audio Analysis

### `audio_analysis/esp32_serial_audio.py` — ESP32 Serial Receiver

**`probe_esp32_uart_stream(port, baud_rate, timeout_sec) → bool`**:  
Opens the serial port with a short read timeout, reads up to `timeout_sec` seconds of data, and checks whether the sync byte pair `0xAA 0x55` appears anywhere in the buffer. Returns `True` if an ESP32 stream is confirmed. This is called before committing to serial audio to avoid the Pi treating an empty (but open) UART as a valid audio source.

**`ESP32SerialAudioReceiver`** (threaded):

- **`__init__`**: Stores port/baud/buffer_seconds; accepts optional callbacks `on_mode_change`, `on_marker`, `on_diag_request`. `oled_missing` flag changes whether `PKT_PI_STATUS` is sent proactively.
- **`start()`**:
  1. Opens `serial.Serial` at 921600 baud, 8N1, 1-second read timeout.
  2. Probes for `0xAA 0x55` sync for up to 2 seconds. Prints either `"Connected ... (sync detected)"` or a WARNING if no sync is found.
  3. Starts two daemon threads: `_receiver_loop` and `_heartbeat_loop`.
- **`_receiver_loop()`**: Continuously reads from the serial port:
  1. Calls `_find_sync()` which scans byte-by-byte for `0xAA 0x55`.
  2. Reads TYPE byte (1 byte), LENGTH bytes (2 bytes LE), DATA (LENGTH bytes), CHECKSUM (1 byte).
  3. If checksum fails: increments `packets_corrupted`, discards.
  4. Dispatches by TYPE:
     - `PKT_AUDIO (0x01)`: Appends data to the ring buffer; trims if buffer exceeds `max_buffer_bytes`.
     - `PKT_MODE_CHANGE (0x02)`: Calls `on_mode_change(data[0])` in the receiver thread.
     - `PKT_MARKER (0x03)`: Calls `on_marker()` in the receiver thread.
     - `PKT_DIAG_REQUEST (0x04)`: Calls `send_pi_status(PI_STATUS_OK, force=True)`, sends `PKT_ACK`, calls `on_diag_request()`.
     - Unknown types: silently ignored (forward-compatible).
- **`_heartbeat_loop()`**: Sends `PKT_HEARTBEAT` (type `0x10`, payload `0x00`) every 3 seconds. If the ESP32 does not receive a heartbeat for 8 seconds, it blinks LED2 in slow 1 Hz pattern.
- **Ring buffer**: `bytearray` trimmed from the front to keep at most `buffer_seconds × 16000 × 2` bytes (60 seconds by default). Thread-safe behind `_buffer_lock`.
- **`get_last_n_seconds(seconds) → bytes`**: Reads the tail of the ring buffer.
- **`write_to_wav(output_path, seconds) → bool`**: Converts raw bytes to `np.int16`, writes a 16 kHz mono WAV via `soundfile`. Requires at least 0.5 seconds of data (Whisper produces garbage on shorter clips).

**`ESP32SerialRecorder`**: Drop-in replacement for `Recorder` using a shared `ESP32SerialAudioReceiver`. The `start()` method clears the receiver's ring buffer and records the start time. The `stop()` method calls `write_to_wav()` to flush the elapsed audio to disk. The `flush_to_wav()` method is called by `LiveCaptioner` between transcription intervals to ensure the file is up-to-date without stopping the recording.

### `audio_analysis/transcription.py` — Audio Recording and Transcription

**`Recorder`** (sounddevice-based):
- Opens a `sounddevice.InputStream` on `start()` and writes PCM samples in the callback directly to a `soundfile.SoundFile` (16 kHz, 16-bit, mono WAV).
- On `stop()`: flushes and closes the file, returns the path.

**`Transcriber`**:
- Wraps `faster_whisper.WhisperModel`.
- Loads the model lazily on the first `transcribe()` call (avoids startup delay until a person actually appears).
- Runs on CPU with `int8` quantization. On Pi 4B (1.8 GHz ARM Cortex-A72), `tiny` model transcribes approximately 10–30 seconds of audio in ~5–15 seconds wall-clock time.
- `transcribe(audio_path) → str`: Returns all segment texts joined with spaces.

**`summarize_text(text, max_sentences) → str`**: Naive extractive summary (split on `.`, join first N sentences). Used as a fallback when the remote LLM is unreachable.

**`summarize_one_sentence(text, max_chars) → str`**: Calls `summarize_text(max_sentences=1)`, clamps to `max_chars` characters without cutting words, appends `…` if truncated.

**`create_recorder(audio_source, serial_port, serial_baud, ...) → Recorder|ESP32SerialRecorder`** (factory):
1. If `audio_source == "auto"`: probes UART for ESP32 sync; picks `"esp32-serial"` or `"sounddevice"` accordingly.
2. If `audio_source == "esp32-serial"`: probes a second time (2-second window) to confirm before committing. Falls back to `Recorder` with a warning if no sync found.
3. If `"esp32-serial"` confirmed: reuses or creates a shared `ESP32SerialAudioReceiver` (one per port/baud via `_shared_serial_receivers` dict with a lock) and returns an `ESP32SerialRecorder` wrapping it.

**`get_shared_receiver(serial_port, serial_baud, **kwargs) → ESP32SerialAudioReceiver`**:
Returns the cached receiver for a given port/baud. Creates and starts one if it does not exist. Extra `kwargs` (callbacks, `oled_missing`) are forwarded only to the constructor, so calling code can register callbacks before the recorder is used.

### `audio_analysis/live_caption.py` — Live Captioner

**`LiveCaptioner`** is called once per main loop iteration:

```python
captioner.update(active_recorders, active_meetings, cursor)
caption = captioner.get_caption_for_present(presence_state)
```

`update()` iterates all active recordings. For each that has not been updated within `interval_sec`:
1. If the recorder is an `ESP32SerialRecorder`, calls `flush_to_wav()` to write current audio to disk. If the flush returned `False` (buffer empty), skips this cycle.
2. Runs `transcriber.transcribe(audio_path)` on the current file.
3. Stores the last `max_words` words as the caption for that person.
4. Updates the `meetings.transcript` column in real time so progress is not lost if the process crashes.

`get_caption_for_present()` returns the caption for the first person with `presence_state == 'present'`, or any available caption as a fallback.

### `audio_analysis/caption_speaker.py` — TTS Speaker

`CaptionSpeaker` runs a single daemon thread that reads from an 8-slot queue. On each dequeue:

1. Enforces a minimum interval of 1.2 s between utterances.
2. If `speak_only_new=True` (default): extracts only the words not in the previous utterance.
3. Sanitizes the text (removes control characters, clamps length).
4. Speaks via the selected backend:
   - **`pyttsx3`** (preferred): Cross-platform TTS engine. Rate and volume are configurable.
   - **`espeak-ng` / `espeak`** (fallback): Shell subprocess. Detected via `shutil.which`.
5. If neither is available, the instance is a no-op (`available = False`).

### `audio_analysis/summarize_meetings.py` — Batch Summarization

Offline script to generate or refresh summaries for existing DB meetings. Reads transcripts and writes summaries using the same `summarize_one_sentence` / `summarize_text` functions. Useful after running the app without a remote summarizer.

### `audio_analysis/backfill_transcripts.py`

Re-runs Whisper on existing WAV files referenced in the `meetings` table. Updates the `transcript` column for rows where it is currently empty. Handy if the app was run with transcription disabled.

---

## 10. OLED Output

**File:** `oled_output/oled_display.py`  
**Environment variables:** `OLED=1` to enable (default: disabled), `OLED_I2C_BUS`, `OLED_I2C_ADDR` (default `0x3C`), `OLED_WIDTH`, `OLED_HEIGHT`, `OLED_DRIVER` (`ssd1306` or `sh1106`), `OLED_FONT`, `OLED_FONT_SIZE`.

**`get_display() → _LumaDisplay | _DummyDisplay`**  
If `OLED=1` is not set in the environment, returns `_DummyDisplay` immediately. Otherwise attempts to initialize luma.oled. Tries the configured address, then `0x3C`, then `0x3D`. On failure, returns `_DummyDisplay`.

**`_LumaDisplay.update_text(lines: List[str])`**  
Throttles to at most one OLED redraw per 250 ms. Renders each string on a new row with a configurable monospace font (default size 12 px). Clamps each line to 64 characters. Supports up to 8 lines.

**`_DummyDisplay`**  
Silent no-op. Used on macOS, when luma.oled is not installed, or when hardware is absent. All `update_text()` calls return without error.

**Display content in normal operation:**

| Situation | Lines shown |
|---|---|
| Startup complete | `["Ready", "HH:MM:SS"]` |
| Known person detected | `[name, "seen N • last HH:MM:SS", prev_summary (if any), "REC" (if recording)]` |
| No face visible | `["No one", "HH:MM:SS"]` |

---

## 11. Data Access Layer

**File:** `data_access/db.py`

`open_db(path=None) → sqlite3.Connection`:  
Opens the database at `DB_PATH` (default: `data_access/faces.db`) and calls `ensure_all_schemas()`, which creates the three tables if they do not exist and runs the `face_embeddings` seed migration (copies any existing `faces.embedding` blobs that have no corresponding rows in `face_embeddings`).

`prune_embeddings_if_needed(conn, face_id, max_count=30)`:  
If a person has more than `max_count` templates, removes the lowest-quality ones. The quality sort key is `(Laplacian_variance, created_at)` ascending, so the worst/oldest are removed first.

`get_latest_finished_meeting(conn, person_id) → tuple | None`:  
Returns `(meeting_id, ended_at, transcript, summary)` for the most recent completed meeting for that person. Used at recognition time to display a previous-meeting summary on the OLED and video overlay.

**File:** `data_access/visualize_db.py`

```
python -m data_access.visualize_db                       # console summary
python -m data_access.visualize_db --html                # write docs/db_report.html
python -m data_access.visualize_db --limit 50 --html     # last 50 meetings
python -m data_access.visualize_db --show-text           # also print transcripts
```

The HTML report is a self-contained single file with embedded CSS. It shows all faces (ID, name, seen count, templates, average quality) and recent meetings (start, end, transcript length, audio path, summary, collapsible transcript text).

---

## 12. Summarization Service

The summarization service is **optional**. If the `SUMMARIZER_URL` environment variable is not set, the system falls back to a purely local extractive summary.

### `summarization/server.py` — FastAPI Service

Started with `make run-summarizer` or `python -m summarization.server`. Listens on `0.0.0.0:8008` by default.

**Endpoints:**

`GET /health` — Returns `{"ok": true, "ollama_url": ..., "ollama_model": ...}`. Use this to verify Ollama is reachable.

`POST /summarize` — Body: `SummarizeRequest` (JSON):
```json
{
  "transcript": "full conversation text",
  "previous_summary": "optional previous summary",
  "person_name": "Alice",
  "max_chars": 140
}
```
Calls Ollama, applies output constraints via `clamp_summary_one_sentence`, returns `{"summary": "...", "model": "...", "took_ms": 123}`.

### `summarization/ollama_client.py`

`generate_one_shot(prompt, cfg) → (text, meta)`:  
POSTs to `http://127.0.0.1:11434/api/generate` (configurable via `OLLAMA_URL`). Uses `stream=false`. Default model: `llama3.1:8b`. Timeout: 30 s. Returns the `response` field of the JSON body plus metadata (timing, token counts).

**Environment variables for Ollama:**

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama API base URL |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model to use |
| `OLLAMA_TEMPERATURE` | `0.2` | Sampling temperature |
| `OLLAMA_TOP_P` | `0.9` | Nucleus sampling |
| `OLLAMA_NUM_PREDICT` | `80` | Max tokens to generate |
| `OLLAMA_TIMEOUT_SEC` | `30` | HTTP request timeout |

### `summarization/prompting.py`

`build_one_sentence_summary_prompt(transcript, previous_summary, person_name, max_chars) → str`:  
Constructs a tightly-constrained prompt asking the LLM to produce exactly one sentence (≤ `max_chars` characters, no quotes/bullets/emojis, grounded in the transcript, consistent with prior context).

### `summarization/remote_client.py`

`remote_summarize_one_sentence(transcript, previous_summary, person_name, max_chars, cfg) → str`:  
Called from `main.py` when `SUMMARIZER_URL` is set. POSTs to `/summarize` via `urllib.request` (no extra dependencies). Raises `RemoteSummarizerError` on network failure or empty response; caller falls back to local `summarize_one_sentence()`.

### `summarization/text.py`

`clamp_summary_one_sentence(text, max_chars) → str`:  
Trims a summary to `max_chars` at a word boundary and appends `…`. Used both in the server (to enforce constraints on LLM output) and in the client (to enforce constraints on responses).

### `summarization/backfill_summaries.py`

Re-generates summaries for meetings that already have transcripts. Supports both local extractive and remote LLM modes. Useful for retroactively adding LLM summaries after setting up Ollama.

---

## 13. Utility & Maintenance Scripts

### `probe_uart.py`

Standalone script that opens the Pi's serial port and scans for `0xAA 0x55` sync bytes to confirm the ESP32 is connected and transmitting. Output: `"ESP32 stream detected"` or `"No sync bytes found after N seconds"`.

### `brightness_gui.py`

Tkinter GUI for adjusting camera brightness/contrast/saturation in real time via OpenCV `VideoCapture` property codes. Useful for tuning camera settings before running the main app.

### `testing/`

| File | Purpose |
|---|---|
| `prove_esp32_packetization.py` | Reads raw bytes from serial, parses packets, reports statistics |
| `test_64x64.py` | Verifies OLED rendering at 64×64 resolution |
| `test_speaker.py` | Plays a test tone via sounddevice to verify audio output |
| `test_transcribe_esp32_uart.py` | End-to-end: capture from ESP32 UART, transcribe with Whisper, print result |

---

## 14. Setup & Installation

### Raspberry Pi 4B (or compatible)

```
bash setup_pi.sh
```

The script performs 7 steps:
1. `apt-get update && apt full-upgrade`
2. Install apt packages: Python venv, make, bzip2, wget, OpenCV (with GStreamer), picamera2, cmake, build-essential, libboost-dev, portaudio19-dev, libsndfile1-dev, espeak-ng, i2c-tools, openblas, lapack, sqlite3
3. Create `.venv` with `--system-site-packages` (required so the venv can see apt-installed `python3-opencv` and `python3-picamera2` without pip re-downloading them)
4. `pip install` all packages inline: numpy<2.0, Pillow, luma.oled, sounddevice, soundfile, pyserial, dlib (compiled from source, ~10-20 min), faster-whisper, pyttsx3, requests
5. Download dlib model files (~120 MB total)
6. Configure UART: `raspi-config nonint do_serial_hw 0`, `do_serial_cons 1`; add `dtoverlay=uart0` and `enable_uart=1` to `/boot/firmware/config.txt`; disable `serial-getty` services; add `dialout`/`tty` udev rules
7. Enable I2C via raspi-config; add user to `i2c` group
8. **Reboot required** to apply UART/I2C/group changes

After reboot, the Makefile's `PY` variable will resolve to `.venv/bin/python` automatically.

### macOS (Development)

```
bash setup_mac.sh
```

Steps:
1. Install Homebrew (if missing)
2. Install system deps via Homebrew: cmake, python@3, portaudio, libsndfile, espeak, wget
3. Create `.venv` (deactivates any active conda/virtualenv first; always recreates to ensure a clean state; uses `/opt/homebrew/bin/python3` when available)
4. `pip install` inline: numpy<2.0, Pillow, opencv-python, sounddevice, soundfile, pyserial, requests, dlib (compiled ~5-10 min), faster-whisper, pyttsx3, fastapi, uvicorn
5. Download dlib model files
6. Create `data/recordings/` directory; run import smoke test

On macOS:
- Camera opens via AVFoundation (built-in webcam, index 0)
- Microphone uses the default audio input via sounddevice
- ESP32 serial audio is Pi-only; `--audio-source auto` falls back to sounddevice
- OLED is Pi-only; `_DummyDisplay` is used silently
- `--no-audio` is useful to skip the sounddevice dependency check if no mic is available

### ESP32 Firmware

1. Install the Arduino IDE or PlatformIO.
2. Install the ESP32 board support package (Espressif Systems).
3. Open `esp32_firmware/truevision_main.ino`.
4. Set `BOARD_PROFILE` at the top of the file (`1` = test board, `2` = production board).
5. Select your ESP32 board variant and the correct COM port.
6. Upload at 921600 baud.

---

## 15. Makefile Targets

Run from the repository root. The Makefile auto-selects `.venv/bin/python` if present, otherwise falls back to `python3`.

| Target | Command | Description |
|---|---|---|
| `make run` | `python main.py` | Full system: face recognition + audio transcription |
| `make run-face` | `python main.py --no-audio --force-mode face` | Face recognition only (no audio recording) |
| `make run-audio` | `python main.py --audio-source esp32-serial --serial-baud 921600 --force-mode audio` | ESP32 audio only, face detection skipped |
| `make run-esp32` | `python main.py --audio-source esp32-serial --serial-baud 921600` | Full system with ESP32 audio; mode switch honored |
| `make run-esp32-force-both` | `python main.py --audio-source esp32-serial --no-mode-gate ...` | Ignore ESP32 mode packets; always run both |
| `make run-overlay-no-audio` | `python main.py --no-audio --overlay-only` | Face overlay on black background, no audio |
| `make run-speak` | `python main.py --speak-captions` | Full system with spoken captions |
| `make run-summarizer` | `python -m summarization.server` | Start the FastAPI summarization service |
| `make summarizer-setup` | `pip install fastapi uvicorn requests` | Install summarizer service deps |
| `make fetch-models` | `python facial_recognition/models/fetch_models.py` | Download dlib model files |
| `make db-report` | `python data_access/visualize_db.py --html --limit 100` | Generate `docs/db_report.html` |
| `make setup-mac` | `bash setup_mac.sh` | Run macOS dev setup |

Environment variables configurable at make time:
- `SUMMARIZER_URL` (default `http://127.0.0.1:8008`) — Set to empty string to disable remote summarization.
- `SUMMARIZER_TIMEOUT_SEC` (default `8`)
- `DB_REPORT_LIMIT` (default `100`)

---

## 16. Runtime Flags Reference

All flags can be set via environment variables (shown in parentheses) in addition to CLI arguments.

### Camera

| Flag | Default | Description |
|---|---|---|
| `--camera-backend` | `auto` | `auto`, `opencv`, `gstreamer`, `picamera2` |
| `--camera-index` | `0` | Device index for OpenCV |
| `--camera-width` | `640` | Requested frame width |
| `--camera-height` | `480` | Requested frame height |
| `--camera-fps` | `30` | Requested frame rate |

### Face Recognizer

| Flag | Default | Description |
|---|---|---|
| `--face-detector` | `auto` | `hog`, `cnn`, or `auto` (CNN if model file present) |
| `--match-threshold` | `0.6` | L2 distance cutoff for recognition (lower = stricter) |
| `--quality-min-var` | `120.0` | Minimum blur score to add a template |
| `--diversity-min-dist` | `0.20` | Minimum L2 distance between templates for the same person |
| `--add-cooldown-sec` | `5.0` | Min seconds between template adds |
| `--absence-grace-sec` | `2.0` | Seconds to wait before declaring a person absent |
| `--template-verbose` | off | Print template add/skip decisions |

### Audio & Transcription

| Flag | Default | Description |
|---|---|---|
| `--audio` / `--no-audio` | on | Enable/disable all audio recording and transcription |
| `--audio-source` | `auto` | `auto`, `sounddevice`, `esp32-serial` |
| `--serial-port` | `/dev/serial0` | UART device path |
| `--serial-baud` | `921600` | UART baud rate |
| `--whisper-model` | `tiny` | `tiny`, `base`, `small`, `medium`, `large-v2`, etc. |
| `--caption-interval` | `0.7` | Seconds between Whisper runs for live captions |
| `--caption-max-words` | `30` | Rolling word window for caption display |
| `--caption-max-lines` | `2` | Max lines in on-screen caption box |

### TTS

| Flag | Default | Description |
|---|---|---|
| `--speak-captions` | off | Speak live captions via TTS |
| `--speech-rate` | `175` | Words per minute |
| `--speech-volume` | `1.0` | Volume (0.0-1.0) |

### ESP32 Mode Gate

| Flag | Default | Description |
|---|---|---|
| `--no-mode-gate` | off | Ignore ESP32 mode packets; always run both face and audio |
| `--force-mode` | (none) | Override to `audio`, `face`, or `both` regardless of ESP32 |

### Summarization

| Flag | Default | Description |
|---|---|---|
| `--summary-async` | off | Run summarization in a background thread (non-blocking) |
| `--summary-max-sentences` | `1` | Sentences in local extractive summary |
| `--summary-max-chars` | `140` | Character cap for session end summary |
| `--prev-summary-max-chars` | `140` | Character cap for previous-meeting summary display |

### Display

| Flag | Default | Description |
|---|---|---|
| `--overlay-only` | off | Draw bounding boxes/text on a black background instead of the camera feed |

---

## 17. End-to-End Execution Flow

This section traces exactly what happens from `python main.py` to a recognized person's summary appearing on screen.

```
python main.py
│
├─ parse_args()
├─ _log_system_diagnostics()  → prints platform, board, throttle, CMA
├─ open_db()                  → opens data_access/faces.db, creates tables
├─ get_display()              → initializes OLED or DummyDisplay
├─ open_camera()              → tries opencv → gstreamer → picamera2
├─ Recognizer()               → loads 3 dlib .dat model files
├─ [if audio] import transcription.*
│   └─ create_recorder / Transcriber / summarize_text loaded
├─ [if esp32-serial or auto]
│   ├─ probe_esp32_uart_stream()  → opens serial port, looks for 0xAA55
│   └─ get_shared_receiver()     → starts ESP32SerialAudioReceiver
│       ├─ ESP32SerialAudioReceiver(port, baud, callbacks)
│       ├─ serial.Serial(921600 baud, 8N1)
│       ├─ 2-second sync probe (prints Connected or WARNING)
│       ├─ _receiver_loop [daemon thread]
│       └─ _heartbeat_loop [daemon thread] → sends 0x10 every 3s
├─ Transcriber()              → WhisperModel loaded lazily on first use
├─ LiveCaptioner()
├─ [if --speak-captions] CaptionSpeaker()
└─ [OLED] "Ready"
│
└─ while True:  ──── MAIN LOOP ────────────────────────────────────────
    │
    ├─ check current_mode[0] != applied_mode
    │   └─ _apply_mode_transition() if changed
    │
    ├─ cap.read()  → BGR frame from camera
    │
    ├─ drain _marker_queue
    │   └─ for each marker: UPDATE meetings SET transcript = transcript || '[MARKER HH:MM:SS]'
    │
    ├─ [if not MODE_AUDIO] recog.detect_and_recognize(conn, frame)
    │   ├─ cvtColor BGR → gray
    │   ├─ dlib HOG/CNN detector  → face rectangles
    │   ├─ for each rect:
    │   │   ├─ shape_predictor  → 68 landmarks
    │   │   ├─ face_rec_model   → 128-float64 embedding
    │   │   ├─ query face_embeddings: L2 distance to all templates
    │   │   └─ return FaceInfo(rect, embedding, person_id, name, ...)
    │   │
    │   └─ for each FaceInfo:
    │       ├─ [if person recognized and was absent]
    │       │   ├─ get_latest_finished_meeting()  → fetch prev summary
    │       │   ├─ recog.update_seen()
    │       │   ├─ _oled_show_person(name, seen_count, prev_summary)
    │       │   └─ _start_session(person_id)
    │       │       ├─ create_recorder()  → Recorder or ESP32SerialRecorder
    │       │       ├─ rec.start()        → open WAV file
    │       │       └─ INSERT INTO meetings (person_id, started_at, audio_path)
    │       │
    │       └─ recog.maybe_add_embedding()  → INSERT face_embeddings if diverse+sharp
    │           └─ prune_embeddings_if_needed() if > 30 templates
    │
    ├─ cv2.rectangle, cv2.putText  → draw overlays
    │
    ├─ [if transcription active] captioner.update(active_recorders, ...)
    │   ├─ [ESP32Recorder] rec.flush_to_wav()  → write ring buffer to .wav
    │   └─ transcriber.transcribe(audio_path)  → Whisper on current file
    │       └─ UPDATE meetings SET transcript = <new text>
    │
    ├─ [if caption] draw caption box at bottom of frame
    │   └─ [if --speak-captions] speaker.submit(caption)
    │       └─ pyttsx3 or espeak in daemon thread
    │
    ├─ absence check
    │   └─ [if person absent > grace_sec] _stop_session(person_id)
    │       ├─ rec.stop()  → finalize WAV
    │       ├─ transcriber.transcribe(full_wav)  → final transcript
    │       ├─ UPDATE meetings SET ended_at, transcript
    │       └─ generate summary:
    │           ├─ [if SUMMARIZER_URL set] remote_summarize_one_sentence()
    │           │   └─ POST /summarize to FastAPI → Ollama → llama3.1
    │           └─ [fallback] summarize_one_sentence()  (extractive)
    │               └─ UPDATE meetings SET summary
    │
    └─ cv2.imshow()  →  press 'q' to quit, 't' to toggle transcription
```

---

## 18. Tuning & Configuration

### Performance on Pi 4B

The Pi 4B is slower than the Pi 5. Key tuning knobs:

| Parameter | Flag | Recommendation |
|---|---|---|
| Whisper model size | `--whisper-model` | `tiny` for real-time; `base` for better accuracy if latency is acceptable |
| Caption interval | `--caption-interval` | 1.5–3.0 on Pi 4B to reduce CPU contention with face detection |
| Face detector | `--face-detector` | `hog` on Pi 4B (CNN is too slow) |
| Audio disable | `--no-audio` | Skip transcription entirely if only face tracking is needed |
| Overlay only | `--overlay-only` | Slightly reduces display compositing cost |
| Camera resolution | `--camera-width 320 --camera-height 240` | Lower resolution significantly speeds up dlib HOG detection |

### Recognition Accuracy

| Parameter | Flag | Effect |
|---|---|---|
| Match threshold | `--match-threshold` | Lower (e.g. 0.5) = fewer false positives. Raise (e.g. 0.7) = more lenient |
| Template diversity | `--diversity-min-dist` | Higher = only highly distinct poses are stored; lower = more variety |
| Template quality | `--quality-min-var` | Higher = only sharp frames added. Raise if motion blur is an issue |
| Cooldown | `--add-cooldown-sec` | Lower to build up templates faster; raise to avoid redundant captures |

### OLED

Enable by adding `OLED=1` to the environment or your systemd service file:

```
Environment=OLED=1
Environment=OLED_I2C_BUS=1
Environment=OLED_I2C_ADDR=0x3C
```

### Summarization

To use Ollama on a separate machine (laptop), expose Ollama's API on all interfaces:

```
OLLAMA_HOST=0.0.0.0 ollama serve
```

Then on the Pi:

```
export SUMMARIZER_URL=http://192.168.1.50:8008
make run-summarizer   # run server.py on the laptop
make run              # Pi will POST to laptop's server
```

---

## 19. Recreating the Project from Scratch

This section provides a complete recipe for someone building TrueVision from zero.

### Hardware Bill of Materials

| Item | Notes |
|---|---|
| Raspberry Pi 4B (2GB or more RAM recommended) | Or Pi 5 (see note below) |
| MicroSD card (32GB+) | Raspberry Pi OS Bookworm 64-bit |
| Pi Camera Module v2 or v3 (CSI) | Or a USB webcam with OpenCV support |
| ESP32 development board | ESP32-WROOM-32 or equivalent |
| SPH0645 or INMP441 MEMS I2S microphone breakout | 3.3V compatible |
| SSD1306 or SH1106 OLED display | 128×64 pixels, I2C, 3.3V |
| 3× jumper wires (ESP32 ↔ Pi UART) | — |
| 4-5× jumper wires (I2S mic to ESP32) | — |
| 2× jumper wires (OLED to Pi I2C) | — |
| Active cooling fan for Pi | Strongly recommended; passive heatsink alone is insufficient under load |

> **Pi 5 note:** The Pi 5 uses a new RP1 I/O chip. Some users have experienced hardware instability under sustained camera + dlib load with early Pi OS Trixie builds. The Pi 4B is currently the more stable platform for this application.

### Step-by-Step Build

**1. Prepare the Pi**

Flash Raspberry Pi OS Bookworm 64-bit to a microSD card using Raspberry Pi Imager. Enable SSH in the imager settings. Boot and SSH in or attach a keyboard/display.

**2. Clone the repository**

```
git clone https://github.com/ahebbani/TrueVision.git
cd TrueVision
```

**3. Run the Pi setup script**

```
bash setup_pi.sh
```

Wait for dlib to compile (~15 min). When the script finishes, reboot:

```
sudo reboot
```

**4. Wire the ESP32 microphone**

Solder or connect the SPH0645/INMP441 to the ESP32:
- BCLK → GPIO 16
- LRCLK/WS → GPIO 17
- Data out → GPIO 5
- SEL/LR pin → GND (select LEFT channel)
- VDD → 3.3V, GND → GND

**5. Wire the ESP32 to the Pi UART**

| ESP32 | Pi |
|---|---|
| GPIO 1 (TX) | Physical pin 10 (GPIO 15 / RXD) |
| GPIO 3 (RX) | Physical pin 8 (GPIO 14 / TXD) |
| GND | Physical pin 6 (GND) |

**6. Flash the firmware**

- Install Arduino IDE and the Espressif ESP32 board package.
- Open `esp32_firmware/truevision_main.ino`.
- Set `BOARD_PROFILE` at the top (1 = test board, 2 = production PCB).
- In Arduino IDE: Tools → Board → "ESP32 Dev Module", Port → your ESP32's serial port.
- Upload.

**7. Wire the OLED to Pi I2C**

| OLED | Pi |
|---|---|
| SDA | Physical pin 3 (GPIO 2) |
| SCL | Physical pin 5 (GPIO 3) |
| VCC | Physical pin 1 (3.3V) |
| GND | Physical pin 6 (GND) |

Verify I2C with: `i2cdetect -y 1` — you should see `3c` appear in the table.

**8. Connect the Pi Camera**

Insert the Pi Camera Module into the CSI ribbon connector. Verify with:

```
libcamera-hello
```

If this shows a preview window, the camera is working.

**9. Enroll faces**

```
source .venv/bin/activate
python -m facial_recognition.add_face
```

Follow the prompts. Repeat for each person.

**10. Run the system**

```
make run
```

Or with ESP32 audio:

```
make run-esp32
```

**11. (Optional) Set up as a systemd service**

```
sudo cp scripts/truevision.service /etc/systemd/system/
sudo systemctl enable truevision.service
sudo systemctl start truevision.service
```

Edit `scripts/truevision.service` to set environment variables like `OLED=1` and `SUMMARIZER_URL` before copying.

**12. (Optional) Summarization**

Install Ollama on a more powerful machine and pull the default model:

```
ollama pull llama3.1:8b
```

Then run the summarization server:

```
make run-summarizer
```

On the Pi, set:

```
export SUMMARIZER_URL=http://<server_ip>:8008
make run
```

---

*End of documentation.*
