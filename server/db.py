"""Server-side SQLite store for backfill jobs and meeting results.

This is a *server-local* database that tracks uploaded audio, transcription
status, and results.  It is NOT the Pi's faces.db — it only holds transient
processing state and finished results that the Pi can poll / fetch.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from server.config import ServerConfig

_cfg = ServerConfig()
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "truevision_server.db")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id    INTEGER NOT NULL,
            audio_path    TEXT,
            status        TEXT NOT NULL DEFAULT 'queued',
            transcript    TEXT,
            summary       TEXT,
            error         TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)"
    )
    conn.commit()


def open_db(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    _ensure_schema(conn)
    return conn


# ── Job helpers ────────────────────────────────────────────────────────────

def create_job(conn: sqlite3.Connection, meeting_id: int, audio_path: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO jobs (meeting_id, audio_path, status) VALUES (?, ?, 'queued')",
        (meeting_id, audio_path),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def store_meeting_result(conn: sqlite3.Connection, meeting_id: int, *,
                         transcript: str = "",
                         summary: str = "",
                         status: str = "done",
                         error: Optional[str] = None) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO jobs (meeting_id, audio_path, status, transcript, summary, error) VALUES (?, NULL, ?, ?, ?, ?)",
        (meeting_id, status, transcript, summary, error),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_job_by_meeting(conn: sqlite3.Connection, meeting_id: int):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, meeting_id, audio_path, status, transcript, summary, error "
        "FROM jobs WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
        (meeting_id,),
    )
    return cur.fetchone()


def get_queued_jobs(conn: sqlite3.Connection, limit: int = 10):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, meeting_id, audio_path FROM jobs WHERE status = 'queued' ORDER BY id LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def update_job(conn: sqlite3.Connection, job_id: int, *,
               status: Optional[str] = None,
               transcript: Optional[str] = None,
               summary: Optional[str] = None,
               error: Optional[str] = None) -> None:
    parts, vals = [], []
    if status is not None:
        parts.append("status = ?"); vals.append(status)
    if transcript is not None:
        parts.append("transcript = ?"); vals.append(transcript)
    if summary is not None:
        parts.append("summary = ?"); vals.append(summary)
    if error is not None:
        parts.append("error = ?"); vals.append(error)
    if not parts:
        return
    parts.append("updated_at = datetime('now')")
    vals.append(job_id)
    conn.cursor().execute(
        f"UPDATE jobs SET {', '.join(parts)} WHERE id = ?", vals
    )
    conn.commit()
