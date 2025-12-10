#!/usr/bin/env python3
"""
Backfill transcripts and summaries for meetings with recorded audio but missing text.

Usage:
  python -m audio_analysis.backfill_transcripts
  # or
  python audio_analysis/backfill_transcripts.py

Options:
  --db <path>           Path to faces.db (default: data_access/faces.db)
  --model <size>        faster-whisper model size (tiny/base/small...) [default: tiny]
  --device <dev>        Device for inference [default: cpu]
  --compute <type>      Compute type [default: int8]
  --limit <n>           Max meetings to process [default: 10]
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# Ensure repo root is importable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from data_access.db import open_db, DB_PATH as DB_PATH_DEFAULT
from audio_analysis.transcription import Transcriber, summarize_text


def main():
    p = argparse.ArgumentParser(description="Backfill transcripts for meetings")
    p.add_argument('--db', default=DB_PATH_DEFAULT, help='Path to faces.db (default: %(default)s)')
    p.add_argument('--model', default='tiny', help='faster-whisper model size (default: %(default)s)')
    p.add_argument('--device', default='cpu', help='Inference device (default: %(default)s)')
    p.add_argument('--compute', default='int8', help='Compute type (default: %(default)s)')
    p.add_argument('--limit', type=int, default=10, help='Max meetings to process (default: %(default)s)')
    args = p.parse_args()

    try:
        tr = Transcriber(model_size=args.model, device=args.device, compute_type=args.compute)
    except Exception as e:
        print(f"ERROR: Could not initialize Transcriber: {e}\nInstall faster-whisper or adjust options.")
        return

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return

    conn = open_db(args.db)
    cur = conn.cursor()

    # Select meetings with audio_path present but transcript empty
    cur.execute(
        """
        SELECT id, audio_path FROM meetings
        WHERE COALESCE(audio_path, '') != '' AND COALESCE(transcript, '') = ''
        ORDER BY id DESC LIMIT ?
        """,
        (args.limit,),
    )
    rows = cur.fetchall()
    if not rows:
        print("No meetings needing transcription.")
        conn.close()
        return

    print(f"Processing {len(rows)} meeting(s)...")
    for mid, audio_path in rows:
        if not audio_path or not os.path.exists(audio_path):
            print(f" - [{mid}] audio missing: {audio_path}")
            continue
        try:
            text = tr.transcribe(audio_path)
        except Exception as e:
            print(f" - [{mid}] transcription failed: {e}")
            text = ''
        summary = summarize_text(text)
        cur.execute(
            "UPDATE meetings SET transcript = ?, summary = ? WHERE id = ?",
            (text, summary, mid),
        )
        conn.commit()
        print(f" - [{mid}] updated (chars={len(text)})")

    conn.close()


if __name__ == '__main__':
    main()
