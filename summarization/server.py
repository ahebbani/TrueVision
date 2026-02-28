from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

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


def create_app() -> FastAPI:
    app = FastAPI(title="TrueVision Summarizer", version="0.1.0")

    @app.get("/health")
    def health():
        cfg = OllamaConfig()
        return {
            "ok": True,
            "ollama_url": cfg.base_url,
            "ollama_model": cfg.model,
        }

    @app.post("/summarize", response_model=SummarizeResponse)
    def summarize(req: SummarizeRequest):
        transcript = (req.transcript or "").strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="transcript is required")

        # Build prompt + call Ollama.
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

        # Enforce display constraints regardless of model compliance.
        summary = clamp_summary_one_sentence(raw, max_chars=int(req.max_chars))
        if not summary:
            # As a fallback, summarize from the transcript directly.
            summary = clamp_summary_one_sentence(transcript, max_chars=int(req.max_chars))

        return SummarizeResponse(
            summary=summary,
            model=str(meta.get("model") or cfg.model),
            took_ms=int(meta.get("took_ms") or 0),
        )

    return app


app = create_app()


if __name__ == "__main__":
    # Convenience runner: python -m summarization.server
    import uvicorn

    host = os.environ.get("SUMMARIZER_HOST", "0.0.0.0")
    port = int(os.environ.get("SUMMARIZER_PORT", "8008"))
    uvicorn.run("summarization.server:app", host=host, port=port, reload=False)
