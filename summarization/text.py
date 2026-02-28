from __future__ import annotations

import re


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def normalize_whitespace(text: str) -> str:
    return " ".join((text or "").replace("\n", " ").split()).strip()


def first_sentence(text: str) -> str:
    text = normalize_whitespace(text)
    if not text:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(text)
    if parts:
        return parts[0].strip()
    return text


def clamp_summary_one_sentence(text: str, max_chars: int = 140) -> str:
    """Ensure output is exactly one short sentence (display-safe)."""
    s = first_sentence(text)
    s = s.strip().strip('"').strip()
    if not s:
        return ""

    # Ensure it ends with sentence punctuation unless we clamp with ellipsis.
    if len(s) <= max_chars:
        if s[-1] not in ".!?":
            s += "."
        return s

    clipped = s[: max_chars + 1]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    clipped = clipped.rstrip(" .!?")
    return clipped + "…"
