"""FastAPI application — the TrueVision server entry-point.

Combines:
- Existing summarization endpoint (backward-compat with ``summarization.server``)
- WebSocket audio streaming + live transcription
- Backfill job REST API
- mDNS advertisement
- Background backfill worker

Run:
    python -m server.app          # production
    uvicorn server.app:app --reload  # development
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from server.config import ServerConfig
from server.audio_ws import get_handler
from server.discovery import DiscoveryAdvertiser
from server import db as server_db
from server.telegram_sender import handle_telegram_voice_command

# Re-use existing summarization helpers
from summarization.ollama_client import OllamaConfig, generate_one_shot
from summarization.prompting import build_one_sentence_summary_prompt
from summarization.text import clamp_summary_one_sentence

# ── Pydantic models (backward-compat with summarization.server) ───────────

class SummarizeRequest(BaseModel):
    transcript: str = Field(..., description="Full transcript text")
    previous_summary: Optional[str] = Field(None)
    person_name: Optional[str] = Field(None)
    max_chars: int = Field(140, ge=40, le=500)


class SummarizeResponse(BaseModel):
    summary: str
    model: str
    took_ms: int


class TelegramRequest(BaseModel):
    command: str


class JobStatusResponse(BaseModel):
    job_id: int
    meeting_id: int
    status: str
    transcript: Optional[str] = None
    summary: Optional[str] = None
    error: Optional[str] = None


# ── Application lifecycle ─────────────────────────────────────────────────

_cfg = ServerConfig()
_advertiser = DiscoveryAdvertiser(_cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs(_cfg.backfill_upload_dir, exist_ok=True)
    _advertiser.start()

    # Pre-warm the Whisper model in a background thread so the first
    # WebSocket connection doesn't block.
    import threading
    threading.Thread(target=lambda: get_handler(_cfg)._ensure_model(),
                     daemon=True, name="whisper-warmup").start()

    # Start backfill worker
    from server.backfill_worker import backfill_loop
    backfill_task = asyncio.create_task(backfill_loop(_cfg))

    yield

    # Shutdown
    backfill_task.cancel()
    _advertiser.stop()


app = FastAPI(title="TrueVision Server", version="1.0.0", lifespan=lifespan)

# ── Health & info ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    ollama_cfg = OllamaConfig()
    gpu_available = False
    try:
        import torch
        gpu_available = torch.cuda.is_available()
    except ImportError:
        pass
    return {
        "ok": True,
        "server_version": "1.0.0",
        "ollama_url": ollama_cfg.base_url,
        "ollama_model": ollama_cfg.model,
        "whisper_model": _cfg.whisper_model,
        "whisper_device": _cfg.whisper_device,
        "gpu_available": gpu_available,
        "services": ["transcription", "summarization", "backfill"],
    }


# ── Summarization (backward-compatible with summarization.server) ─────────

@app.post("/summarize", response_model=SummarizeResponse)
def summarize(req: SummarizeRequest):
    transcript = (req.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="transcript is required")

    cfg = OllamaConfig()
    prompt = build_one_sentence_summary_prompt(
        transcript=transcript,
        previous_summary=req.previous_summary,
        person_name=req.person_name,
        max_chars=int(req.max_chars),
    )

    try:
        raw, meta = generate_one_shot(prompt, cfg=cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ollama call failed: {e}")

    summary = clamp_summary_one_sentence(raw, max_chars=int(req.max_chars))
    if not summary:
        summary = clamp_summary_one_sentence(transcript, max_chars=int(req.max_chars))

    return SummarizeResponse(
        summary=summary,
        model=str(meta.get("model") or cfg.model),
        took_ms=int(meta.get("took_ms") or 0),
    )


@app.post("/telegram")
def telegram(req: TelegramRequest):
    command = (req.command or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required")

    try:
        result = handle_telegram_voice_command(command)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"telegram failed: {e}")

    return {"ok": True, "result": result}


@app.post("/telegram_llm")
def telegram_llm(req: TelegramRequest):
    transcript = (req.command or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="command is required")

    prompt = f"""
Extract the Telegram message from this voice command.

Return ONLY the final message text.
No JSON. No explanation. No quotes.

Voice command:
{transcript}
"""

    try:
        cfg = OllamaConfig()
        cfg.model = "llama3.1:8b"

        message, _meta = generate_one_shot(prompt, cfg=cfg)
        message = (message or "").strip().strip('"').strip()

        print("LLM RAW MESSAGE:", message)

        if not message:
            raise RuntimeError("LLM returned empty message")

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ollama failed: {e}")

    try:
        result = handle_telegram_voice_command("send " + message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"telegram failed: {e}")

    return {
        "ok": True,
        "message": message,
        "result": result,
        "model": cfg.model,
    }


# ── WebSocket audio streaming ────────────────────────────────────────────

@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    handler = get_handler(_cfg)
    # Default session key — overridden by session_start message
    current_session_key: Optional[int] = None
    print("[ws/audio] Client connected")

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.receive":
                if "bytes" in msg and msg["bytes"]:
                    # Binary frame: raw PCM audio
                    data = msg["bytes"]
                    sk = current_session_key if current_session_key is not None else 0
                    handler.append_audio(sk, data)

                    # Attempt live captioning
                    caption = handler.maybe_caption(sk)
                    if caption:
                        await ws.send_text(json.dumps({
                            "type": "caption",
                            "session_key": sk,
                            "text": caption,
                        }))

                elif "text" in msg and msg["text"]:
                    # Text frame: JSON control message
                    try:
                        ctrl = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue

                    msg_type = ctrl.get("type", "")

                    if msg_type == "session_start":
                        sk = int(ctrl.get("session_key", 0))
                        current_session_key = sk
                        print(f"[ws/audio] session_start received for session {sk}")
                        handler.start_session(
                            sk,
                            person_id=ctrl.get("person_id"),
                            meeting_id=ctrl.get("meeting_id"),
                        )

                    elif msg_type == "session_end":
                        sk = int(ctrl.get("session_key", current_session_key or 0))
                        meeting_id = ctrl.get("meeting_id")
                        print(
                            f"[ws/audio] session_end received for session {sk} "
                            f"meeting={meeting_id}"
                        )
                        # Run final transcription + summarization
                        loop = asyncio.get_event_loop()
                        transcript = await loop.run_in_executor(
                            None, handler.final_transcribe, sk,
                        )
                        summary = ""
                        if transcript.strip():
                            summary = await loop.run_in_executor(
                                None,
                                handler.summarize,
                                transcript,
                                ctrl.get("previous_summary", ""),
                                ctrl.get("person_name"),
                                int(ctrl.get("max_chars", 140)),
                            )
                        if meeting_id is not None:
                            try:
                                conn = server_db.open_db()
                                server_db.store_meeting_result(
                                    conn,
                                    int(meeting_id),
                                    transcript=transcript,
                                    summary=summary,
                                    status="done",
                                )
                                conn.close()
                                print(f"[ws/audio] Stored result for meeting {meeting_id} in server DB")
                            except Exception as e:
                                print(f"[ws/audio] Failed to persist meeting result {meeting_id}: {e}")
                        await ws.send_text(json.dumps({
                            "type": "result",
                            "session_key": sk,
                            "meeting_id": meeting_id,
                            "transcript": transcript,
                            "summary": summary,
                        }))
                        print(
                            f"[ws/audio] Result sent for session {sk}: "
                            f"{len(transcript)} transcript chars, {len(summary)} summary chars"
                        )
                        handler.end_session(sk)

            elif msg["type"] == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        print("[ws/audio] Client disconnected")
    except Exception as e:
        print(f"[ws/audio] Connection error: {e}")
    finally:
        # Clean up any remaining sessions for this connection
        if current_session_key is not None:
            handler.end_session(current_session_key)


# ── Backfill REST API ────────────────────────────────────────────────────

@app.post("/api/meetings/{meeting_id}/audio")
async def upload_audio(meeting_id: int, file: UploadFile = File(...)):
    """Pi uploads a WAV file for backfill transcription."""
    os.makedirs(_cfg.backfill_upload_dir, exist_ok=True)
    dest = os.path.join(_cfg.backfill_upload_dir, f"meeting_{meeting_id}.wav")
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    conn = server_db.open_db()
    job_id = server_db.create_job(conn, meeting_id, dest)
    conn.close()

    return {"job_id": job_id, "meeting_id": meeting_id, "status": "queued"}


@app.get("/api/meetings/{meeting_id}/status", response_model=JobStatusResponse)
def meeting_status(meeting_id: int):
    """Pi polls for transcript/summary result."""
    conn = server_db.open_db()
    row = server_db.get_job_by_meeting(conn, meeting_id)
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no job found for this meeting")
    job_id, mid, audio_path, status, transcript, summary, error = row
    return JobStatusResponse(
        job_id=job_id,
        meeting_id=mid,
        status=status,
        transcript=transcript,
        summary=summary,
        error=error,
    )


@app.post("/api/backfill/trigger")
def trigger_backfill():
    """Manually trigger backfill processing (the worker will pick up jobs
    on its next iteration)."""
    return {"ok": True, "message": "backfill worker will process queued jobs on next cycle"}


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=_cfg.host,
        port=_cfg.port,
        reload=False,
    )
