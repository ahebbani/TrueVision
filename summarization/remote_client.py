from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from summarization.text import clamp_summary_one_sentence


@dataclass
class RemoteSummarizerConfig:
    url: str = (
        os.environ.get("SUMMARIZER_URL", "")
        or os.environ.get("TRUEVISION_SERVER_URL", "")
    ).rstrip("/")
    timeout_sec: float = float(os.environ.get("SUMMARIZER_TIMEOUT_SEC", "8"))
    max_chars: int = int(os.environ.get("SUMMARIZER_MAX_CHARS", "140"))


class RemoteSummarizerError(RuntimeError):
    pass


def remote_summarize_one_sentence(
    transcript: str,
    previous_summary: Optional[str] = None,
    person_name: Optional[str] = None,
    max_chars: Optional[int] = None,
    cfg: Optional[RemoteSummarizerConfig] = None,
) -> str:
    cfg = cfg or RemoteSummarizerConfig()
    if not cfg.url:
        raise RemoteSummarizerError("SUMMARIZER_URL / TRUEVISION_SERVER_URL is not set")

    payload = {
        "transcript": transcript or "",
        "previous_summary": previous_summary,
        "person_name": person_name,
        "max_chars": int(max_chars if max_chars is not None else cfg.max_chars),
    }

    req = urllib.request.Request(
        url=f"{cfg.url}/summarize",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=float(cfg.timeout_sec)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout) as e:
        raise RemoteSummarizerError(f"remote summarizer request failed: {e}")

    try:
        data = json.loads(raw) if raw else {}
    except Exception as e:
        raise RemoteSummarizerError(f"invalid JSON from summarizer: {e}")

    summary = (data.get("summary") or "").strip()
    summary = clamp_summary_one_sentence(summary, max_chars=int(payload["max_chars"]))
    if not summary:
        raise RemoteSummarizerError("empty summary returned")
    return summary
