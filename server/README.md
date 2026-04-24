server
=======

Purpose
- FastAPI-based server that accepts WebSocket audio streams, runs transcription
  and summarization, and supports backfill processing for uploaded WAVs.

Key modules
- `app.py` — FastAPI application entrypoint. Exposes:
  - `/ws/audio` WebSocket endpoint for binary PCM frames and JSON control frames
    (`session_start`, `session_end`). Handles live captions and returns final
    transcript/summary in `result` messages.
  - `/summarize` HTTP POST endpoint — compatibility wrapper that builds an Ollama
    prompt and returns a short one-sentence summary.
  - `/api/meetings/{id}/audio` and `/api/meetings/{id}/status` for backfill uploads
    and polling.
- `audio_ws.py` — WS handler implementation used by `app.py` (handler factory `get_handler`).
- `backfill_worker.py` — Background loop that processes queued backfill jobs (WAV → transcript).
- `db.py` — server-side DB helpers to store jobs and meeting results (separate from
  the client `data_access` DB).
- `discovery.py` — mDNS advertiser used for local network discovery.
- `config.py` — `ServerConfig` containing ports, whisper model config, and storage paths.

How the flow works
- A Pi connects to `/ws/audio`, sends `session_start` with a session key and then
  streams PCM frames; server runs transcription and sends `caption` frames back for
  live display and a `result` message when `session_end` is received.

Deployment
- Run with `uvicorn server.app:app`. Use the `scripts/truevision-server.service` template
  to run under systemd on the server host.
