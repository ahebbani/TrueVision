"""Backward-compatible shim for the production TrueVision server.

Historically, some workflows launched ``python -m summarization.server``.
The actual production server now lives in ``server.app`` and handles both
live audio captioning/translation and summarization.
"""
from __future__ import annotations

import os

from server.app import app


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("SUMMARIZER_HOST", os.environ.get("TRUEVISION_SERVER_HOST", "0.0.0.0"))
    port = int(os.environ.get("SUMMARIZER_PORT", os.environ.get("TRUEVISION_SERVER_PORT", "8008")))
    uvicorn.run("summarization.server:app", host=host, port=port, reload=False)
