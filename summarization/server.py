from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from summarization.audio_ws import AudioServerConfig, AudioTranscriptionHandler
from summarization.ollama_client import OllamaConfig, generate_one_shot
from summarization.prompting import build_one_sentence_summary_prompt
from summarization.text import clamp_summary_one_sentence


class SummarizeRequest(BaseModel):
    transcript: str = Field(..., description="Full transcript text for the most recent meeting")
    previous_summary: Optional[str] = Field(None, description="Prior conversation summary for this person")
    person_name: Optional[str] = Field(None, description="Optional name to improve specificity")
    max_chars: int = Field(140, ge=40, le=500, description="Maximum characters for the returned sentence")


class SummarizeResponse(BaseModel):
    summary: str
    model: str
    took_ms: int


_audio_cfg = AudioServerConfig()
_audio_handler = AudioTranscriptionHandler(_audio_cfg)


def _summarize_transcript(
    transcript: str,
    previous_summary: Optional[str] = None,
    person_name: Optional[str] = None,
    max_chars: int = 140,
) -> SummarizeResponse:
    transcript = (transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="transcript is required")

    cfg = OllamaConfig()
    prompt = build_one_sentence_summary_prompt(
        transcript=transcript,
        previous_summary=previous_summary,
        person_name=person_name,
        max_chars=int(max_chars),
    )

    try:
        raw, meta = generate_one_shot(prompt, cfg=cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ollama call failed: {e}")

    summary = clamp_summary_one_sentence(raw, max_chars=int(max_chars))
    if not summary:
        summary = clamp_summary_one_sentence(transcript, max_chars=int(max_chars))

    return SummarizeResponse(
        summary=summary,
        model=str(meta.get("model") or cfg.model),
        took_ms=int(meta.get("took_ms") or 0),
    )


def create_app() -> FastAPI:
    app = FastAPI(title="TrueVision Summarizer", version="0.1.0")

    @app.get("/health")
    def health():
        cfg = OllamaConfig()
        return {
            "ok": True,
            "ollama_url": cfg.base_url,
            "ollama_model": cfg.model,
            "whisper_model": _audio_cfg.whisper_model,
            "services": ["summarization", "transcription", "translation"],
        }

    @app.post("/summarize", response_model=SummarizeResponse)
    def summarize(req: SummarizeRequest):
        return _summarize_transcript(
            transcript=req.transcript,
            previous_summary=req.previous_summary,
            person_name=req.person_name,
            max_chars=int(req.max_chars),
        )

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket):
        await ws.accept()
        current_session_key: Optional[int] = None

        try:
            while True:
                msg = await ws.receive()

                if msg["type"] == "websocket.receive":
                    if "bytes" in msg and msg["bytes"]:
                        if current_session_key is None:
                            continue
                        _audio_handler.append_audio(current_session_key, msg["bytes"])
                        caption = _audio_handler.maybe_caption(current_session_key)
                        if caption:
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "caption",
                                        "session_key": current_session_key,
                                        "text": caption["text"],
                                        "source_language": caption.get("source_language"),
                                    }
                                )
                            )
                    elif "text" in msg and msg["text"]:
                        try:
                            ctrl = json.loads(msg["text"])
                        except json.JSONDecodeError:
                            continue
                        msg_type = ctrl.get("type", "")

                        if msg_type == "session_start":
                            current_session_key = int(ctrl.get("session_key", 0))
                            _audio_handler.start_session(
                                current_session_key,
                                person_id=ctrl.get("person_id"),
                                meeting_id=ctrl.get("meeting_id"),
                            )
                        elif msg_type == "session_end":
                            session_key = int(ctrl.get("session_key", current_session_key or 0))
                            loop = asyncio.get_running_loop()
                            transcript = await loop.run_in_executor(
                                None,
                                _audio_handler.final_transcribe,
                                session_key,
                            )
                            summary = ""
                            if transcript.strip():
                                try:
                                    summary_resp = await loop.run_in_executor(
                                        None,
                                        _summarize_transcript,
                                        transcript,
                                        ctrl.get("previous_summary"),
                                        ctrl.get("person_name"),
                                        int(ctrl.get("max_chars", 140)),
                                    )
                                    summary = summary_resp.summary
                                except Exception:
                                    summary = ""
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "result",
                                        "session_key": session_key,
                                        "meeting_id": ctrl.get("meeting_id"),
                                        "transcript": transcript,
                                        "summary": summary,
                                    }
                                )
                            )
                            _audio_handler.end_session(session_key)
                            if current_session_key == session_key:
                                current_session_key = None
                elif msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if current_session_key is not None:
                _audio_handler.end_session(current_session_key)

    return app


app = create_app()


if __name__ == "__main__":
    # Convenience runner: python -m summarization.server
    import uvicorn

    host = os.environ.get("SUMMARIZER_HOST", "0.0.0.0")
    port = int(os.environ.get("SUMMARIZER_PORT", "8008"))
    uvicorn.run("summarization.server:app", host=host, port=port, reload=False)
