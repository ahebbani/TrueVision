from __future__ import annotations

from typing import Optional


def build_one_sentence_summary_prompt(
    transcript: str,
    previous_summary: Optional[str] = None,
    person_name: Optional[str] = None,
    max_chars: int = 140,
) -> str:
    """Prompt tuned for a short, display-safe summary.

    Notes:
    - We avoid chain-of-thought requests.
    - We make the output format strict.
    """
    who = (person_name or "the person").strip() or "the person"
    prev = (previous_summary or "").strip()
    transcript = (transcript or "").strip()

    # Keep the prompt explicit and compact.
    return (
        "You are generating a short memory cue to show when a face is recognized.\n"
        "Task: Write exactly ONE sentence summarizing the most recent conversation,\n"
        "grounded in the transcript, and (if provided) consistent with the previous conversation context.\n\n"
        f"Hard requirements:\n"
        f"- Exactly one sentence.\n"
        f"- Must be <= {int(max_chars)} characters.\n"
        f"- No quotes, no bullet points, no emojis, no headings.\n"
        f"- Prefer concrete topics/decisions over filler.\n\n"
        f"Person: {who}\n"
        f"Previous conversation summary (may be empty): {prev if prev else '[none]'}\n\n"
        f"Transcript (may be long):\n{transcript}\n\n"
        "Return only the one-sentence summary and nothing else."
    )
