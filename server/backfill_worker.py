"""Background worker that processes queued backfill transcription jobs.

Runs as an asyncio task inside the FastAPI event loop.  Periodically polls
the server-local SQLite for jobs with status='queued', transcribes the audio
via Whisper, summarizes via Ollama, and stores the result.
"""
from __future__ import annotations

import asyncio
import os
import traceback
from typing import Optional

from server.config import ServerConfig
from server import db as server_db
from server.audio_ws import get_handler


async def backfill_loop(cfg: Optional[ServerConfig] = None) -> None:
    cfg = cfg or ServerConfig()
    print(f"[backfill] Worker started — polling every {cfg.backfill_interval_sec}s")

    conn = server_db.open_db()

    while True:
        try:
            jobs = server_db.get_queued_jobs(conn, limit=5)
            for job_id, meeting_id, audio_path in jobs:
                if not audio_path or not os.path.isfile(audio_path):
                    server_db.update_job(conn, job_id, status="error",
                                         error=f"audio file not found: {audio_path}")
                    continue

                server_db.update_job(conn, job_id, status="processing")
                print(f"[backfill] Processing job {job_id} (meeting {meeting_id})")

                handler = get_handler(cfg)
                try:
                    transcript = handler._transcribe_wav(audio_path)
                except Exception as e:
                    server_db.update_job(conn, job_id, status="error",
                                         error=f"transcription failed: {e}")
                    print(f"[backfill] Transcription error for job {job_id}: {e}")
                    continue

                summary = ""
                if transcript.strip():
                    try:
                        summary = handler.summarize(transcript)
                    except Exception as e:
                        print(f"[backfill] Summarization error for job {job_id}: {e}")

                server_db.update_job(conn, job_id, status="done",
                                     transcript=transcript, summary=summary)
                print(f"[backfill] Completed job {job_id}: "
                      f"{len(transcript)} chars transcript, {len(summary)} chars summary")

        except Exception as e:
            print(f"[backfill] Worker error: {e}")
            traceback.print_exc()

        await asyncio.sleep(cfg.backfill_interval_sec)
