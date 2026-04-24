TrueVision
===============

TrueVision is an on-device + server-assisted system for realtime face recognition,
live captions from an ESP32 microphone, transcription, and short meeting summarization.

Top-level components
- `main.py`: Pi-side orchestrator combining the camera, face recognizer, ESP32 audio,
  local Whisper transcription (optional), and server offload. Drives presence state,
  session start/stop, overlays, and DB updates.
- `setup_pi.sh`, `setup_server.sh`, `Makefile`: setup and convenience scripts for
  installing the runtime on a Raspberry Pi and the server host.
- `test_server_audio_ws.py`, `test_translate_mac.py`: small test utilities.

Architecture overview
- Device (Pi): runs `main.py`, performs face recognition using dlib, records audio
  (ESP32 or local mic), optionally runs local Whisper for transcription and summarization,
  and persists face templates and meeting metadata in an SQLite DB under `data_access`.
- Server: a FastAPI app in `server/` exposing WebSocket audio ingestion, transcription,
  summarization (via Ollama or local heuristics), and a backfill worker for uploaded audio.
- Summarization: utilities under `summarization/` provide prompt building, Ollama integration,
  and helpers to clamp or post-process short one-sentence summaries.

Where to start
- Run the server: `python -m server.app` (or via the provided systemd service template in `scripts/`).
- Run the Pi/desktop app: `python main.py` with relevant flags (see `--help`).

Project layout
- `audio_analysis/` — ESP32 serial audio support, live captioning, transcription helpers.
- `data/` — persistent recordings (created at runtime).
- `data_access/` — SQLite DB creation and helper functions used by both Pi and server.
- `facial_recognition/` — camera helpers, dlib-based recognizer, embedding management.
- `server/` — FastAPI server, WebSocket audio handler, backfill worker, DB utilities.
- `summarization/` — prompt and model glue for generating short summaries.
- `docs/` and `esp32_firmware/` — documentation and ESP32 firmware sketches.

License & notes
- This README is generated from repository code for developer orientation. See `docs/` for setup notes.
