#!/usr/bin/env python3
"""
Generate or refresh meeting summaries from existing transcripts.

Usage:
  python -m audio_analysis.summarize_meetings
  # or
  python audio_analysis/summarize_meetings.py

Options:
  --db <path>            Path to faces.db (default: data_access/faces.db)
  --limit <n>            Max meetings to process (default: 25)
  --max-sentences <n>    Max sentences in summary (default: 5)
  --force                Recompute summaries even if one already exists
"""
from __future__ import annotations

import argparse
import os
import sys

# Ensure repo root importability when run as a script
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from data_access.db import open_db, DB_PATH as DB_PATH_DEFAULT
from audio_analysis.transcription import summarize_text


def main():
    p = argparse.ArgumentParser(description="Summarize meetings from transcripts")
    p.add_argument('--db', default=DB_PATH_DEFAULT, help='Path to faces.db (default: %(default)s)')
    p.add_argument('--limit', type=int, default=25, help='Max meetings to process (default: %(default)s)')
    p.add_argument('--max-sentences', type=int, default=5, help='Max sentences in summary (default: %(default)s)')
    p.add_argument('--force', action='store_true', help='Recompute summaries even if present')
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return

    conn = open_db(args.db)
    cur = conn.cursor()

    if args.force:
        cur.execute(
            """
            SELECT id, COALESCE(transcript,'') FROM meetings
            WHERE COALESCE(transcript, '') != ''
            ORDER BY id DESC LIMIT ?
            """,
            (args.limit,),
        )
    else:
        cur.execute(
            """
            SELECT id, COALESCE(transcript,'') FROM meetings
            WHERE COALESCE(transcript, '') != '' AND COALESCE(summary, '') = ''
            ORDER BY id DESC LIMIT ?
            """,
            (args.limit,),
        )

    rows = cur.fetchall()
    if not rows:
        print("No meetings need summarization.")
        conn.close()
        return

    print(f"Summarizing {len(rows)} meeting(s)...")
    updated = 0
    for mid, transcript in rows:
        if not transcript:
            continue
        summary = summarize_text(transcript, max_sentences=args.max_sentences)
        cur.execute(
            "UPDATE meetings SET summary = ? WHERE id = ?",
            (summary, mid),
        )
        conn.commit()
        updated += 1
        print(f" - [{mid}] summary len={len(summary)}")

    conn.close()
    print(f"Done. Updated {updated} meeting(s).")


if __name__ == '__main__':
    main()
