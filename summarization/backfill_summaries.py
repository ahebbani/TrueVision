#!/usr/bin/env python3
"""Backfill 1-sentence meeting summaries for existing transcripts.

This is intended to run on the off-device machine where the summarizer service
(FastAPI + Ollama) is available.

Usage:
  python -m summarization.backfill_summaries --db data_access/faces.db

Env:
  SUMMARIZER_URL=http://127.0.0.1:8008
  SUMMARIZER_TIMEOUT_SEC=30
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from data_access.db import DB_PATH as DB_PATH_DEFAULT, open_db
from summarization.remote_client import RemoteSummarizerConfig, remote_summarize_one_sentence


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill 1-sentence summaries for meetings")
    p.add_argument("--db", default=DB_PATH_DEFAULT, help="Path to faces.db (default: %(default)s)")
    p.add_argument("--limit", type=int, default=200, help="Max meetings to process (default: %(default)s)")
    p.add_argument("--force", action="store_true", help="Recompute even if summary exists")
    p.add_argument("--max-chars", type=int, default=140, help="Clamp summary to this many chars (default: %(default)s)")
    p.add_argument("--url", default=None, help="Override summarizer base URL (default: $SUMMARIZER_URL)")
    p.add_argument("--timeout", type=float, default=None, help="Override timeout seconds (default: $SUMMARIZER_TIMEOUT_SEC)")
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return 2

    cfg = RemoteSummarizerConfig()
    if args.url:
        cfg.url = str(args.url).rstrip("/")
    if args.timeout is not None:
        cfg.timeout_sec = float(args.timeout)

    if not cfg.url:
        print("SUMMARIZER_URL is not set (or pass --url).")
        return 2

    conn = open_db(args.db)
    cur = conn.cursor()

    if args.force:
        cur.execute(
            """
            SELECT m.id, m.person_id, COALESCE(f.name,''), COALESCE(m.transcript,''), COALESCE(m.summary,'')
            FROM meetings m
            LEFT JOIN faces f ON f.id = m.person_id
            WHERE COALESCE(m.transcript,'') != ''
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (int(args.limit),),
        )
    else:
        cur.execute(
            """
            SELECT m.id, m.person_id, COALESCE(f.name,''), COALESCE(m.transcript,''), COALESCE(m.summary,'')
            FROM meetings m
            LEFT JOIN faces f ON f.id = m.person_id
            WHERE COALESCE(m.transcript,'') != '' AND COALESCE(m.summary,'') = ''
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (int(args.limit),),
        )

    rows = cur.fetchall()
    if not rows:
        print("No meetings need backfill.")
        conn.close()
        return 0

    print(f"Backfilling {len(rows)} meeting(s) via {cfg.url} …")
    updated = 0
    failed = 0

    for mid, pid, name, transcript, existing_summary in rows:
        transcript = (transcript or "").strip()
        if not transcript:
            continue

        # Provide previous summary context for better continuity.
        prev_summary = ""
        try:
            cur.execute(
                """
                SELECT COALESCE(summary,'')
                FROM meetings
                WHERE person_id = ? AND ended_at IS NOT NULL AND id < ? AND COALESCE(summary,'') != ''
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(pid), int(mid)),
            )
            r = cur.fetchone()
            prev_summary = (r[0] if r else "") or ""
        except Exception:
            prev_summary = ""

        try:
            summary = remote_summarize_one_sentence(
                transcript=transcript,
                previous_summary=prev_summary,
                person_name=(name or None),
                max_chars=int(args.max_chars),
                cfg=cfg,
            )
            cur.execute("UPDATE meetings SET summary = ? WHERE id = ?", (summary, int(mid)))
            conn.commit()
            updated += 1
            print(f" - [{mid}] pid={pid} len={len(summary)}")
        except Exception as e:
            failed += 1
            print(f" - [{mid}] pid={pid} FAILED: {e}")

    conn.close()
    print(f"Done. Updated {updated}, failed {failed}.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
