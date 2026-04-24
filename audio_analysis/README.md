audio_analysis
===============

Purpose
- Utilities for handling audio on-device: ESP32 serial audio input, forwarding audio
  to a server, live captioning, offline transcription, and helpers for backfilling
  transcripts.

Key modules
- `transcription.py` — Local transcription wrapper around `faster_whisper` when
  available. Exposes `Transcriber` (background model loading), `transcribe()`,
  `transcribe_live()`, and simple summarizers `summarize_text()` and `summarize_one_sentence()`.
- `esp32_serial_audio.py` — (ESP32 integration) provides an `ESP32SerialAudioReceiver`
  and `ESP32SerialRecorder` to read PCM from an ESP32 over UART and produce WAV files.
- `audio_forwarder.py` — Runs in the Pi process and streams raw PCM to the server over
  WebSocket. Also receives live caption frames and final session results; key API:
  `AudioForwarder.start()`, `send_session_start()`, `send_session_end()`,
  `get_latest_caption()`, `get_result()`.
- `server_connection.py` — Client-side wrapper to check server availability and
  provide connection metadata (used by `main.py` for offload decisions).
- `live_caption.py` — `LiveCaptioner` class that periodically flushes recent audio
  and requests short live transcriptions for on-screen captions. Runs a worker
  thread, keeps per-session status, and writes interim transcripts into the DB.
- `backfill_transcripts.py` / `summarize_meetings.py` — utilities for processing
  historic recordings and generating meeting summaries (used in backfill workflows).

How it integrates
- `main.py` imports `create_recorder()` and `Transcriber` from `transcription.py`.
- When server offload is active, `audio_forwarder.AudioForwarder` streams audio and
  receives transcription/summaries; otherwise, `Transcriber` performs local transcription.

Notes
- Many modules are tolerant to missing optional dependencies (e.g., faster-whisper,
  websocket-client, pyserial) to allow running core components without heavy installs.
