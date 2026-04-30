# TrueVision — Complete Feature Specification

## Project Overview

TrueVision is a wearable assistive-technology system that combines **real-time facial recognition** with **live audio transcription** to help users identify people and recall previous conversations. The Pi outputs to an **AR optic via HDMI** (an OLED-based see-through display where black pixels are transparent, creating a heads-up overlay on the real world). It consists of three hardware/software components:

1. **ESP32 Microcontroller** — Captures audio via I2S microphone and streams it over UART to the Pi. Has a physical two-position switch to toggle between AUDIO and FACE modes.
2. **Raspberry Pi 5 (Edge Device)** — Runs the main application: camera-based facial recognition, audio recording/transcription, live captions, voice-command processing, and an OpenCV display overlay output to the AR display. Also hosts a lightweight web-based phone controller app. This is the central hub.
3. **Server (Linux Desktop/GPU machine)** — Optional offload target that runs a FastAPI server providing GPU-accelerated Whisper transcription, Ollama-based LLM summarization, live caption streaming with translation, Telegram voice-command processing, and live caption streaming back to the Pi over WebSocket.

---

## System Architecture & Connections

### ESP32 → Raspberry Pi 5 (UART)

- **Physical connection**: ESP32 TX pin → Pi RX pin, plus shared GND.
- **Baud rate**: 921600 (configurable).
- **Direction**: TX-only from ESP32 to Pi. The Pi never sends data back.
- **Protocol**: Custom binary framing protocol:
  - Frame: `[0xAA][0x55][TYPE(1)][LEN_LO][LEN_HI][DATA(LEN)][CHECKSUM]`
  - Checksum = `sum(data bytes) & 0xFF`
  - Packet types:
    - `0x01` AUDIO_DATA — raw int16 PCM at 16 kHz mono (256 samples per packet)
    - `0x02` MODE_CHANGE — 1 byte payload: `0x00` = AUDIO mode, `0x01` = FACE mode
    - `0x03` MARKER — timestamp marker event (button press)
- **Audio always streams** regardless of mode. The Pi decides what to do with it based on the current mode.

### Raspberry Pi 5 → Server (HTTP + WebSocket, over LAN)

- **Discovery**: The Pi finds the server via explicit URL (`TRUEVISION_SERVER_URL` env var or `--server-url` flag), or via **mDNS/Zeroconf** autodiscovery (service type `_truevision._tcp.local.`).
- **Health check**: Pi periodically pings `GET /health` on the server to determine availability.
- **Audio forwarding**: When the server is available, the Pi opens a **WebSocket** connection to `ws://<server>/ws/audio` and forwards raw PCM audio in binary frames. JSON text frames are used for session control messages.
- **Results**: The server sends back live captions and final transcription+summary results over the same WebSocket connection.

### Server Services

- **FastAPI** application on port 8008 (configurable).
- **Endpoints**: `/health`, `/summarize` (POST), `/ws/audio` (WebSocket), `/telegram` (POST), `/telegram_llm` (POST), `/api/meetings/<id>/audio` (POST upload), `/api/meetings/<id>/status` (GET poll), `/api/backfill/trigger` (POST).
- **mDNS advertisement**: Advertises itself as `TrueVision Server._truevision._tcp.local.` so Pis on the same LAN discover it automatically.

### Phone → Raspberry Pi 5 (HTTP, over LAN/Wi-Fi)

- The Pi runs a lightweight **web-based controller app** (HTML/JS served over HTTP) accessible from any phone on the same network.
- Acts as a remote control for mode switching (replacing or supplementing the ESP32 physical switch).
- Provides "speed dial" style launcher buttons for quickly opening content on the AR display.
- Connection: phone browser → `http://<pi-ip>:<controller-port>/`.

---

## Operating Modes

The system has three operating modes, controlled by the ESP32's physical switch and server availability:

### 1. FACE Mode (Default)
- Camera is active; facial recognition runs every frame.
- Audio is **not** recorded or transcribed (audio stream from ESP32 is ignored).
- Detected faces are matched against the database; presence tracking is active.
- OpenCV overlay shows bounding boxes, names, seen counts, last-seen timestamps, and previous conversation summaries.

### 2. AUDIO Mode
- Camera feed is still read (for display) but facial recognition is **paused**.
- Audio recording begins immediately into a standalone session (no person association).
- Live captions are displayed as a closed-captioning overlay at the bottom of the screen.
- Transcription happens either locally (via Whisper on-device) or via the server.

### 3. BOTH Mode (Dual Mode — requires server)
- **Only activates when the server is reachable** and handling audio offload.
- Facial recognition runs on the Pi (using the camera).
- Audio is simultaneously forwarded to the server for transcription/captioning.
- Per-person audio sessions: each recognized person gets their own recording and meeting record with a transcript and summary.
- If the server becomes unavailable, the system gracefully falls back to the last single mode (AUDIO or FACE).

### Mode Switching Logic
- The ESP32 sends MODE_CHANGE packets when the physical switch changes position.
- The **phone controller app** can also switch modes via HTTP API, acting as a software alternative to the physical switch.
- The Pi can **force** a mode via `--force-mode audio|face|both`, ignoring ESP32 packets.
- When the server is available and the ESP32 (or phone app) requests AUDIO or FACE, the Pi automatically upgrades to BOTH mode (face on Pi + audio on server).
- When the server goes down, the system falls back to the last requested single mode.

---

## ESP32 Firmware Features

The ESP32 runs an **Arduino sketch** (`.ino` file):

### Audio Capture
- Uses **I2S** peripheral in master/RX mode to read from an I2S MEMS microphone (e.g., INMP441).
- Sample rate: **16,000 Hz**, mono, 32-bit I2S samples right-shifted to 16-bit PCM.
- Buffer size: 256 samples per packet.
- I2S pin assignments: SCK=GPIO 8, WS=GPIO 6, SD=GPIO 7. Microphone SEL/LR tied to GND.

### UART Transmission
- Uses `HardwareSerial(2)` on TX=GPIO 17, RX=GPIO 18.
- Baud rate: 921,600.
- Continuously reads I2S audio and sends framed AUDIO_DATA packets to the Pi.
- Audio **always streams** — the Pi decides whether to use it based on the current mode.

### Mode Switch
- Two-position physical switch wired to **GPIO 35** (AUDIO side) and **GPIO 36** (FACE side).
- Pins 35/36 are input-only on ESP32; external pull-ups/pull-downs required.
- Debounce: 50ms.
- On switch change, sends a MODE_CHANGE packet over UART.
- Reads initial switch state at boot and sends the initial mode.

### Debug LEDs
- **Heartbeat LED** (GPIO 9): Toggles every 500ms to show firmware is running.
- **Packet LED** (GPIO 10): Brief pulse on each audio packet send.

### Design Constraints
- **Single-threaded**: Runs in Arduino `setup()` + `loop()`, no FreeRTOS tasks.
- **TX-only**: Never reads from UART RX. No heartbeat or acknowledgment from Pi.

---

## Raspberry Pi 5 Features

### Facial Recognition

#### Camera Backend
- On Raspberry Pi: Uses **Picamera2** (libcamera) requesting BGR888 frames at configurable resolution (default 640×480).
- On desktop/Mac: Falls back to **OpenCV VideoCapture** with platform-appropriate backend (AVFoundation on macOS).
- Auto-detection based on `platform.system()` and `platform.machine()`.

#### Face Detection
- Uses **dlib** for face detection.
- Two detector modes:
  - **HOG** (Histogram of Oriented Gradients) — fast, CPU-friendly. Default on ARM/Pi.
  - **CNN** (Convolutional Neural Network) — more accurate. Default on x86 with GPU. Requires `mmod_human_face_detector.dat`.
- Auto-selection based on platform, or forced via `--face-detector hog|cnn`.

#### Face Recognition
- Uses **dlib's 128-dimensional face embedding** model (`dlib_face_recognition_resnet_model_v1.dat`).
- Shape prediction via `shape_predictor_68_face_landmarks.dat` (68-point facial landmark model).
- Matching: Computes L2 distance between live embedding and all stored templates. Match threshold default: **0.6**.
- Returns the closest match below threshold.

#### Template Management (Adaptive Embedding Collection)
- The system continuously collects new face embeddings for recognized people to improve accuracy over time.
- **Bootstrap phase** (< 5 templates): Relaxed thresholds — lower quality requirements (Laplacian variance ≥ 60), shorter cooldown (0.75s), lower diversity threshold (L2 ≥ 0.08).
  - Force-bootstrap (< 3 templates): Skips diversity check entirely.
- **Steady-state phase** (≥ 5 templates): Stricter thresholds — quality variance ≥ 120, cooldown 5s, diversity L2 ≥ 0.20.
- **Quality metric**: Laplacian variance of the grayscale face region (blur detection).
- **Pruning**: Max 30 templates per person. When exceeded, lowest-quality templates are removed first.

#### Presence Tracking
- Tracks per-person presence state: `present` or `absent`.
- **Absence grace period** (default 2 seconds): A person must disappear from the frame for this duration before being marked absent. This smooths transient detection misses.
- When a person transitions to `present`: increments their seen count, updates last-seen timestamp, starts an audio session (in BOTH mode), and fetches their previous conversation summary.
- When a person transitions to `absent`: stops their audio session (triggers transcription and summarization).

#### Face Enrollment

Faces can be added to the recognition database in three ways:

##### 1. Voice Command Enrollment (requires server)
- While in FACE or BOTH mode with a single unrecognized face visible, the user says a voice command with the wake word, e.g., *"Assistant, remember this face as John"* or *"TrueVision, this is Sarah."*
- The Pi detects the wake word in the transcript, sends the command to the server's `/telegram_llm`-style endpoint for LLM extraction of the name.
- The system captures the current unrecognized face's embedding and saves it to the database with the extracted name.
- A confirmation overlay (e.g., "Saved: John") briefly appears on the AR display.
- If multiple unrecognized faces are visible, the system ignores the command (ambiguous target). If zero unrecognized faces are visible, the command is also ignored.

##### 2. Phone App Enrollment (via phone controller)
- The phone controller app has an **"Add Face"** button/section.
- When tapped, the phone app shows a text field where the user types the person's name.
- On submit, the Pi captures the current frame, detects the largest unrecognized face, computes the embedding, and saves it with the provided name.
- The phone app shows a success/failure confirmation.
- This method does NOT require the server — it runs entirely on the Pi.

##### 3. CLI Script (standalone, for setup)
- `add_face.py`: Opens the camera, detects a face, computes an embedding, and saves it with a name.
- Interactive: shows a live preview with bounding box; user presses 's' to save.
- Intended for initial database population before deploying the wearable.

### Audio Transcription

#### ESP32 Serial Audio Receiver
- Background daemon thread reads from the UART serial port.
- Parses the binary protocol, validates checksums, and dispatches packets by type.
- Audio data is stored in a thread-safe **ring buffer** (default 60 seconds capacity).
- Handles serial reconnection with exponential backoff on errors.
- Shared singleton per port: multiple subsystems (recorder, forwarder) share the same receiver instance.
- Provides `get_last_n_seconds()`, `get_all_audio()`, `clear_buffer()`, `write_to_wav()` APIs.

#### Recording
- `ESP32SerialRecorder`: Drop-in recorder that wraps the serial receiver.
- `start(directory, prefix)` clears the buffer and begins a session, returning a WAV path.
- `stop()` writes all buffered audio to WAV and returns the file path.
- `flush_to_wav(seconds)` writes the current buffer to a temporary WAV for live captioning without stopping the recording.
- Minimum 0.5 seconds of audio required before writing (avoids Whisper errors on tiny clips).

#### Local Transcription (On-Device)
- Uses **faster-whisper** (CTranslate2-based Whisper implementation).
- Default model: `tiny` (suitable for Pi's limited resources). Configurable via `--whisper-model`.
- Device: CPU with int8 compute type.
- Model loads eagerly in a background thread to avoid blocking the main loop.
- `transcribe(audio_path)` — full transcription of a WAV file.
- `transcribe_live(audio_path, language)` — optimized for live captioning: beam_size=1, no VAD filter, no timestamps, no condition on previous text.

#### Live Captioning (Local)
- `LiveCaptioner` runs a background worker thread for non-blocking transcription.
- Periodically (default every 0.7 seconds) flushes the latest audio window (default 2 seconds) to a temp WAV and transcribes it.
- Displays the last N words (default 30) as a rolling caption.
- Updates the meeting transcript in the database incrementally.
- Manages per-session caption state with generation counters to avoid stale results.

#### Local Summarization (Fallback)
- Simple extractive summarizer: splits text on periods and returns the first N sentences.
- `summarize_one_sentence()`: returns a single sentence clamped to max_chars (default 140). Clips at word boundaries with ellipsis if needed.
- Used as fallback when the server/LLM summarizer is unavailable.

### Server Connection & Audio Offloading

#### Server Discovery
- `ServerConnection` class manages server availability.
- Checks for server via:
  1. Explicit URL from `--server-url` or `TRUEVISION_SERVER_URL` env var.
  2. **mDNS/Zeroconf** autodiscovery for `_truevision._tcp.local.`.
- Background health-check thread pings `/health` every N seconds (default 5).
- Startup: retries N times (default 3) with delay (default 2s) before declaring no server.
- Thread-safe `is_available` property.

#### Audio Forwarding (Pi → Server)
- `AudioForwarder` reads new audio from the serial receiver's ring buffer every ~32ms and sends it as binary WebSocket frames.
- Sends JSON control messages for session lifecycle:
  - `session_start`: includes session_key, person_id, meeting_id.
  - `session_end`: includes previous_summary, person_name, max_chars for summarization context.
- Receives JSON messages back:
  - `caption`: live caption text for display.
  - `result`: final transcript + summary after session ends.
- Reconnection: Up to 3 attempts with 2-second backoff. After exhaustion, waits for the next server health cycle to retry.

#### Remote Summarization Client
- `remote_summarize_one_sentence()` calls `POST /summarize` on the server.
- Sends transcript, previous_summary, person_name, max_chars.
- Falls back to local extractive summarizer on failure.

### AR HUD (Heads-Up Display)

When launched with `make run`, the Pi renders a **full-screen black-background HUD** via OpenCV on the HDMI output. The black background is critical — on the OLED AR optic, black pixels are transparent, so all HUD elements appear to float over the user's real-world view.

The HUD is a single OpenCV window running fullscreen. All elements are rendered with `cv2.putText` / `cv2.rectangle` / `cv2.line` on a black frame each tick. The layout is divided into fixed zones:

#### HUD Layout

```
┌──────────────────────────────────────────────────────────┐
│ TOP-LEFT: Clock & Date          TOP-RIGHT: System Status │
│ 11:42 AM                        CPU 52°C  │ WiFi ▂▅▇    │
│ Wed, Apr 30                     Server: ● Connected      │
│                                  Mode: BOTH              │
├──────────────────────────────────────────────────────────┤
│                                                          │
│               CENTER: Face Recognition Zone              │
│                                                          │
│          ┌──────────┐                                    │
│          │  ┌────┐  │  "John" (seen 14x)                 │
│          │  │face│  │  Last: 2 min ago                   │
│          │  └────┘  │  "Discussed project deadline."     │
│          └──────────┘  ● REC                             │
│                                                          │
├──────────────────────────────────────────────────────────┤
│ BOTTOM-LEFT: Reminders          BOTTOM: Live Captions    │
│ 🔔 Buy milk                     "...and then we decided  │
│ 🔔 Call Sarah at 3pm             to push the deadline."  │
│                                  (Spanish) translated... │
└──────────────────────────────────────────────────────────┘
```

#### Top-Left: Clock & Date
- **Time**: Current local time in 12-hour format (e.g., `11:42 AM`). Updates every frame.
- **Date**: Day of week and date (e.g., `Wed, Apr 30`). Rendered below the time.
- White text, medium font. Always visible regardless of mode.

#### Top-Right: System Status
- **CPU temperature**: Reads from `/sys/class/thermal/thermal_zone0/temp` on the Pi. Displayed in °C. Color-coded: green (< 60°C), yellow (60–75°C), red (> 75°C).
- **Wi-Fi signal strength**: Reads from `/proc/net/wireless` or `iwconfig`. Displayed as a signal bar icon or dBm value.
- **Server connection**: Green dot + "Connected" when the DGX server is reachable, red dot + "Disconnected" when not.
- **Current mode**: Shows the active mode (`FACE`, `AUDIO`, or `BOTH`).
- Small white text, right-aligned. Always visible.

#### Center: Face Recognition Zone
- Active in **FACE** and **BOTH** modes.
- Face bounding boxes: green rectangle around each detected face.
- Name label + seen count (e.g., `John (seen 14x)`).
- Last-seen relative time (e.g., `Last: 2 min ago`).
- Previous conversation summary in yellow text, truncated to 48 chars.
- `● REC` indicator (red) when actively recording audio for that person (BOTH mode).
- Overlays persist for 0.25 seconds after detection to smooth transient misses.
- "Unknown" label for unrecognized faces.

#### Bottom-Center: Live Captions
- Active in **AUDIO** and **BOTH** modes.
- White text on a semi-transparent dark strip at the bottom of the screen.
- Shows the last N words (default 30) as a rolling caption. Word-wraps to fit screen width, shows last 2 lines.
- **Translation indicator**: When the server is translating non-English speech, captions are prefixed with the source language, e.g., `(Spanish) Hello, how are you?`. This tells the user the original language. Server-only — local captioning does not translate.

#### Bottom-Left: Active Reminders
- Shows the most recent active voice notes/reminders (max 3 visible at a time).
- Each reminder is a single line of white text with a bell icon prefix.
- Reminders fade out (or are dismissed) after the user marks them done via the phone app.
- If no active reminders exist, this zone is empty (black = transparent).

#### Confirmation Toasts
- Brief confirmation messages appear center-screen and fade after ~2 seconds.
- Examples: `✓ Saved: John`, `✓ Telegram sent`, `✓ Reminder saved: Buy milk`.
- White text with a subtle semi-transparent background strip.

#### HUD Behavior by Mode
- **FACE mode**: Clock, date, system status, face recognition zone, reminders. No captions.
- **AUDIO mode**: Clock, date, system status, live captions, reminders. No face boxes.
- **BOTH mode**: All zones active simultaneously.
- **Speed-dial content**: When a speed-dial launcher is active (news, YouTube, etc.), the HUD is replaced by the launched browser in fullscreen. The phone app's "close" button returns to the HUD.

### Resource Handling on the Pi
- **Lazy initialization**: Face recognizer, Whisper model, and live captioner are only initialized when first needed in their respective mode. Avoids loading large models that won't be used.
- **Shared serial receiver**: A singleton per port ensures the ESP32 serial connection is opened only once and shared by the recorder, forwarder, and mode-change callbacks.
- **Background model loading**: Whisper model loads in a background thread so the main loop (and camera display) aren't blocked.
- **Graceful cleanup**: On exit (q key, SIGTERM, KeyboardInterrupt), all active recording sessions are stopped and finalized, the audio forwarder and captioner are stopped, the server connection is closed, and the camera is released.
- **Mode transitions**: Switching modes properly stops all sessions from the previous mode before starting new ones. Switching from FACE to AUDIO stops all face-tracking sessions. Switching from AUDIO to FACE stops the audio-only session and clears captions.

---

## Server Features

### FastAPI Application

#### Health Endpoint (`GET /health`)
- Returns server version, Ollama config (URL, model), Whisper model/device, GPU availability, and list of available services.
- Used by the Pi's `ServerConnection` to detect availability.

#### Summarization Endpoint (`POST /summarize`)
- Accepts: transcript, previous_summary, person_name, max_chars.
- Uses **Ollama** LLM (default model: `llama3.1:8b`) to generate a one-sentence summary.
- Prompt is specifically tuned to produce a short "memory cue" suitable for display when a face is recognized. Requirements: exactly one sentence, ≤ max_chars, no quotes/bullets/emojis, prefer concrete topics over filler.
- Falls back to extractive summarization if Ollama fails.

#### WebSocket Audio Endpoint (`/ws/audio`)
- Receives binary frames (raw PCM audio) and JSON control messages from the Pi.
- **Live captioning**: Periodically transcribes a sliding window of recent audio (default 2 seconds) and sends caption text back to the Pi.
- **Final transcription**: On session_end, transcribes the full accumulated audio buffer.
- **Summarization**: After final transcription, generates a one-sentence summary via Ollama.
- **Translation support**: Can detect non-English speech (Spanish, German) and translate to English using Whisper's translate task. Requires a multilingual Whisper model (not `.en` suffix). Configurable source languages and detection confidence threshold.
- **Language-labeled captions**: When translation is active, the server includes the detected source language in the caption payload (e.g., `source_language: "es"`). The caption text is prefixed with a human-readable label like `(Spanish)` or `(German)` so the user knows the original language. The server formats this via `format_caption(text, source_language)` using a label map (`es` → "Spanish", `de` → "German", etc.).
- Sends results back as JSON: `{type: "result", transcript, summary, meeting_id}` and `{type: "caption", text, session_key, source_language}`.
- Stores results in the server's local SQLite database for polling.

#### Telegram Voice Command Endpoints

##### `POST /telegram`
- Accepts a `command` string (the raw voice command text after wake-word extraction).
- Parses the command: strips "send " prefix if present, then sends the remaining text as a Telegram message.
- Uses the **Telegram Bot API** (`/sendMessage`) with a configured bot token and chat ID.
- Returns the Telegram message ID on success.

##### `POST /telegram_llm`
- Accepts a `command` string (the full raw transcript including conversational filler).
- Uses **Ollama LLM** to extract the intended Telegram message from the noisy voice transcript. The LLM prompt asks it to return ONLY the final message text with no JSON, explanation, or quotes.
- After LLM extraction, sends the cleaned message via the Telegram Bot API.
- Returns the extracted message, Telegram result, and model used.
- This endpoint exists because voice transcripts are messy — the LLM cleans up "uh, assistant, can you send a telegram saying hello to everyone" into just "hello to everyone".

#### Backfill API
- `POST /api/meetings/<id>/audio`: Pi uploads a WAV file for offline transcription.
- `GET /api/meetings/<id>/status`: Pi polls for results.
- Background worker (asyncio task) periodically processes queued jobs.

### Whisper Configuration (Server)
- Default model: `small` (more capable than Pi's `tiny`).
- Device: `auto` (tries CUDA first, falls back to CPU).
- Compute type: `float16` on CUDA, `int8` on CPU.
- Automatic CUDA fallback: If CUDA backend fails (missing libraries), gracefully falls back to CPU.

### mDNS Advertisement
- Uses **Zeroconf** to advertise the server on the LAN.
- Service type: `_truevision._tcp.local.`
- Broadcasts local IP address, port, Whisper model, and device info.
- Auto-detects local IP via UDP socket trick.

### Ollama Integration
- Communicates with a local **Ollama** instance via `POST /api/generate` (stream=false).
- Configurable: URL, model, temperature (0.2), top_p (0.9), num_predict (80), timeout (30s).
- All config via environment variables: `OLLAMA_URL`, `OLLAMA_MODEL`, etc.

---

## Telegram Voice Commands

The system supports hands-free messaging via **Telegram** using voice commands. This feature requires the server.

### How It Works (End-to-End Flow)
1. The user says a command containing a **wake word** ("assistant" or "truevision"), e.g., *"Hey assistant, send a telegram to the group saying I'll be 5 minutes late."*
2. After the audio session ends (person leaves frame in BOTH mode, or mode switches away from AUDIO), the Pi transcribes the audio.
3. The Pi checks the transcript for the wake word ("assistant" or "truevision"). If found, it extracts everything after the wake word as the command.
4. The Pi sends the command to the server's `/telegram_llm` endpoint.
5. The server uses **Ollama LLM** to extract the clean Telegram message from the noisy voice transcript.
6. The server sends the cleaned message to a Telegram group/chat via the **Telegram Bot API**.
7. The server returns confirmation to the Pi.

### Wake Word Detection
- Scans the transcript for "assistant" or "truevision" (case-insensitive).
- Everything after the wake word is treated as the command.
- If no wake word is found, no Telegram action is taken.

### Telegram Bot Configuration
- The server holds the **bot token** and **chat ID** (configured in the Telegram sender module).
- Uses the Telegram Bot API `sendMessage` endpoint.
- The Pi never contacts Telegram directly — all Telegram communication goes through the server.

### When Commands Are Processed
- In **AUDIO mode**: When the audio-only session ends (mode switch away from audio), the full transcript is checked for the wake word.
- In **BOTH mode**: When a person's meeting session ends (they leave the frame), their transcript is checked.
- Commands are only processed from **completed** transcripts, not from live captions.

---

## Voice Notes & Reminders

The system supports hands-free voice notes and reminders via the same wake-word pipeline used for Telegram commands.

### How It Works
1. The user says a command with the wake word, e.g., *"Assistant, remind me to buy milk"* or *"TrueVision, note: project deadline is Friday."*
2. After the audio session ends, the Pi detects the wake word and extracts the command.
3. The Pi (or server, if connected) uses keyword matching or LLM extraction to identify the intent as a note/reminder (vs. a Telegram send, face enrollment, etc.).
4. The note text is saved to the Pi's local `notes` table in the SQLite database.
5. A confirmation toast appears on the AR HUD: `✓ Reminder saved: Buy milk`.

### Intent Routing
The wake-word command pipeline supports multiple intents. After extracting the command text, the system routes it based on keywords:
- Commands containing **"telegram"** or **"send"** → Telegram voice command.
- Commands containing **"remember this face"** or **"this is"** → Face enrollment.
- Commands containing **"remind"**, **"note"**, **"remember"** (without "face") → Voice note/reminder.
- If the server is connected, the LLM can be used for more robust intent classification.

### Display on AR HUD
- Active reminders are shown in the **bottom-left zone** of the HUD (max 3 visible at a time).
- Each reminder is a single line with a bell icon prefix.
- Reminders are always visible regardless of mode (FACE, AUDIO, or BOTH).

### Management via Phone App
- The phone controller app has a **"Notes & Reminders"** section.
- View all saved notes/reminders in a scrollable list.
- Mark reminders as **done** (removes from HUD) or **delete** them.
- Add new notes manually by typing on the phone.

### Storage
- Notes are stored in the Pi's SQLite database (`faces.db`) in a `notes` table.
- Fields: id, content, created_at, is_done, dismissed_at.
- Notes marked as done are hidden from the HUD but kept in the database for history.

---

## Live Caption Translation (Server-Only)

When connected to the server, the system can **automatically detect non-English speech and translate it to English** in real-time captions.

### Feature Details
- **Server-only**: This feature is only available when the Pi is connected to the server (DGX Spark or similar GPU machine). Local-only captioning on the Pi does NOT translate.
- **Supported source languages**: Spanish (`es`) and German (`de`) by default. Configurable via `TRANSLATION_SOURCE_LANGUAGES` env var (comma-separated language codes).
- **Target language**: English (`en`). Configurable via `TRANSLATION_TARGET_LANGUAGE`.
- **Requires multilingual Whisper model**: The server must use a Whisper model without the `.en` suffix (e.g., `small`, `medium`, `large`). English-only models (e.g., `small.en`) cannot translate.

### Language Detection
- Whisper automatically detects the spoken language and reports a confidence probability.
- The system caches the detected language for the session once the confidence exceeds the threshold (default: 0.65, configurable via `TRANSLATION_DETECTION_MIN_PROBABILITY`).
- If the detected language is in the supported source list, translation mode activates for that session.
- If the language switches back to English (or an unsupported language) with high confidence, translation mode deactivates.

### Caption Display with Language Indicator
- When translation is active, captions are **prefixed with the source language name** in parentheses:
  - `(Spanish) Hello, how are you?`
  - `(German) The meeting starts at noon.`
- This indicator tells the AR display user that the original speech was in a different language and has been translated.
- When no translation is happening (English speech), captions display without any prefix.
- The language label uses human-readable names: `es` → "Spanish", `de` → "German". Unknown language codes show the code itself capitalized.

### Translation Mechanism
- Uses **Whisper's built-in translate task**: first detects the language with a normal `transcribe` pass, then if a supported non-English language is detected, runs a second pass with `task="translate"` and the detected source language hint.
- This produces English text directly from foreign-language audio without needing a separate translation model.

---

## Phone Controller App

The Pi hosts a **lightweight web-based controller app** that can be accessed from any phone (or device) on the same network. It provides a touch-friendly interface for controlling the TrueVision system without needing the ESP32's physical switch.

### Overview
- **Served by the Pi**: A small HTTP server on the Pi serves a single-page HTML/JS/CSS app.
- **Access**: Open `http://<pi-ip>:<port>/` in any phone browser. No app install required.
- **Purpose**: Remote control for mode switching and quick-launch of content on the AR display.
- **Design**: Dark theme with large, touch-friendly buttons optimized for one-handed phone use.

### Mode Control
- **FACE / AUDIO / BOTH** mode buttons that send commands to the Pi's main application.
- Acts as a software replacement for the ESP32's physical two-position switch.
- Reflects the current active mode with visual feedback (highlighted active button).
- Mode changes from the phone app have the same effect as ESP32 mode-switch packets.

### Speed Dial Launcher
- A grid of large "speed dial" style buttons for quickly launching content on the Pi's HDMI output (the AR display).
- **Available launchers**:
  - **News**: Opens a news feed/page on the AR display.
  - **Weather**: Displays current weather information on the AR display.
  - **Database**: Opens the TrueVision database report (faces, meetings, transcripts) in the AR display.
  - **Instagram**: Opens Instagram in the Pi's browser, displayed on the AR display.
  - **YouTube**: Opens YouTube in the Pi's browser, displayed on the AR display.
- The Pi launches the requested content in a full-screen browser window (e.g., Chromium in kiosk mode) on its HDMI output.
- All launched content uses a **black background** where possible to take advantage of the OLED AR display's transparency.
- A "close/back" button on the phone app dismisses the launched content and returns to the TrueVision camera/caption overlay.

### Face Enrollment
- An **"Add Face"** button opens a simple enrollment panel.
- The user types a name into a text field and taps "Save".
- The Pi captures the current frame, detects the largest unrecognized face, computes its embedding, and saves it to the database under the provided name.
- Success/failure feedback is shown on the phone. A brief "Saved: <name>" confirmation also appears on the AR display.
- Does NOT require the server — runs entirely on the Pi.

### Communication
- The phone app sends HTTP requests to the Pi's controller server.
- The Pi's controller server either adjusts the main application's mode, launches/closes browser windows, or performs face enrollment on the HDMI output.
- The controller does NOT require the DGX server — it runs entirely on the Pi.

---

## Database

### Pi Database (`faces.db` — SQLite)

#### `faces` Table
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment person ID |
| name | TEXT | Person's name |
| embedding | BLOB | Initial face embedding (legacy; kept for seeding) |
| created_at | TEXT | Timestamp of creation |
| last_seen_at | TEXT | Last time this person was detected |
| seen_count | INTEGER | Total number of detection events |

#### `face_embeddings` Table
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment template ID |
| face_id | INTEGER FK | References faces.id |
| embedding | BLOB | 128-dim float64 face descriptor (1024 bytes) |
| created_at | TEXT | When this template was collected |
| quality | REAL | Laplacian variance quality score |

#### `meetings` Table
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment meeting ID |
| person_id | INTEGER FK | References faces.id |
| started_at | TEXT | Session start timestamp |
| ended_at | TEXT | Session end timestamp |
| audio_path | TEXT | Path to WAV recording |
| transcript | TEXT | Full transcription text |
| summary | TEXT | One-sentence summary |

#### `notes` Table
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment note ID |
| content | TEXT | The note/reminder text |
| created_at | TEXT | When the note was created |
| is_done | INTEGER | 0 = active, 1 = completed/dismissed |
| dismissed_at | TEXT | When the note was marked done (nullable) |

### Server Database (`truevision_server.db` — SQLite)

#### `jobs` Table
| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Job ID |
| meeting_id | INTEGER | Pi-side meeting ID |
| audio_path | TEXT | Path to uploaded WAV |
| status | TEXT | `queued`, `processing`, `done`, `error` |
| transcript | TEXT | Transcription result |
| summary | TEXT | Summary result |
| error | TEXT | Error message if failed |
| created_at | TEXT | Job creation time |
| updated_at | TEXT | Last update time |

---

## CLI & Utility Scripts

### `add_face.py`
- Interactive face enrollment: opens camera, detects face, saves embedding with a name.

### `manage_embeddings.py`
- `--stats`: Show template counts and average quality per person.
- `--prune <person_id> --keep <N>`: Remove lowest-quality templates down to N.
- `--delete <person_id>`: Delete all templates for a person.

### `visualize_db.py`
- Console or HTML report of database contents (faces, meetings, transcripts, summaries).

### `backfill_transcripts.py`
- Batch transcribe meetings that have audio files but missing transcripts.

### `summarize_meetings.py`
- Batch generate summaries from existing transcripts.

### `backfill_summaries.py`
- Batch generate summaries via the remote server's Ollama endpoint.

---

## Setup & Deployment

### Raspberry Pi Setup (`setup_pi.sh`)
- Installs system packages: Python3, OpenCV, Picamera2, cmake, dlib build deps, audio libs, GStreamer.
- Creates a Python venv with `--system-site-packages` (required for apt-installed OpenCV/Picamera2).
- Pip installs: numpy, Pillow, soundfile, pyserial, dlib (built from source ~15 min), faster-whisper, requests, websocket-client, zeroconf.
- Downloads dlib model files (~200MB): shape_predictor_68_face_landmarks.dat, dlib_face_recognition_resnet_model_v1.dat.
- Configures UART: enables serial hardware, disables serial console, adds dtoverlay=uart0 for Pi 5, disables serial-getty services, sets up udev rules, adds user to dialout group.
- Reboots after completion.

### Server Setup (`setup_server.sh`)
- Targets Linux desktop/server with optional NVIDIA GPU.
- Installs system packages, detects GPU/CUDA.
- Creates venv, installs: FastAPI, uvicorn, websockets, requests, numpy, soundfile, faster-whisper, zeroconf.
- Installs **Ollama** and pulls default model (`llama3.1:8b`).
- Verifies all imports.

### Systemd Services
- `truevision.service`: Runs `make run-face` on the Pi at boot.
- `truevision-server.service`: Runs `python -m server.app` on the server at boot, after Ollama.

### Makefile Targets
- `make run` — Default run with auto-detection, honors esp32 mode switch.
- `make run-audio` — Force audio mode.
- `make run-face` — Force face mode.
- `make run-force-both` — Request BOTH mode (even without server connection)
- `make run-server` / `make run-server-dev` — Start the server.
- `make db` — Generate HTML database report.
- `make setup-pi` / `make setup-server` — Run setup scripts.

---

## Key Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `TRUEVISION_SERVER_URL` | (empty) | Server URL for audio offload |
| `TRUEVISION_SERVER_PORT` | 8008 | Server listen port |
| `WHISPER_MODEL` | `tiny` (Pi) / `small` (server) | Whisper model size |
| `WHISPER_DEVICE` | `cpu` (Pi) / `auto` (server) | Inference device |
| `WHISPER_COMPUTE_TYPE` | `int8` (Pi) / `float16` (server) | Compute precision |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama API URL |
| `OLLAMA_MODEL` | `llama3.1:8b` | LLM model for summarization |
| `FACE_DETECTOR` | `auto` | Face detector: auto/hog/cnn |
| `SUMMARIZER_URL` | (follows server URL) | Remote summarizer endpoint |
| `CAPTION_INTERVAL_SEC` | 0.7 | Seconds between caption updates |
| `CAPTION_WINDOW_SEC` | 2.0 | Rolling audio window for captions |
| `TRANSLATION_SOURCE_LANGUAGES` | `es,de` | Languages to auto-translate (server-only) |
| `TRANSLATION_TARGET_LANGUAGE` | `en` | Translation target language |
| `TRANSLATION_DETECTION_MIN_PROBABILITY` | 0.65 | Min confidence for language detection |
| `TELEGRAM_BOT_TOKEN` | (configured in code) | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | (configured in code) | Target Telegram chat/group ID |

---

## Dependencies

### ESP32
- Arduino framework with ESP-IDF I2S driver (`driver/i2s.h`).

### Raspberry Pi
- Python 3, OpenCV, dlib, numpy, Pillow, pyserial, soundfile, faster-whisper, requests, websocket-client, zeroconf, picamera2 (on Pi).

### Server
- Python 3, FastAPI, uvicorn, websockets, requests, numpy, soundfile, faster-whisper, zeroconf, Ollama (external).

### Phone Controller App
- No dependencies — pure HTML/CSS/JavaScript served by the Pi. Works in any modern mobile browser.
