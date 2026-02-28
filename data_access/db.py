"""Centralized SQLite database utilities for TrueVision.

This module encapsulates:
- Location of the faces database file
- Functions to ensure all required tables exist
- Embedding pruning logic

It intentionally keeps paths compatible with existing layout so behavior does not change.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional, Sequence

# Database file path (keep existing location under facial_recognition/database/)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Place database file alongside this module for simplicity
DB_PATH = os.path.join(BASE_DIR, "faces.db")

MAX_TEMPLATES_PER_PERSON = 30

# --- Schema ensure helpers -------------------------------------------------

def ensure_faces_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT,
            last_seen_at TEXT,
            seen_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Backfill created_at where missing
    cur.execute("UPDATE faces SET created_at = datetime('now') WHERE created_at IS NULL")
    conn.commit()


def ensure_face_embeddings_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS face_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_id INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            quality REAL,
            FOREIGN KEY(face_id) REFERENCES faces(id)
        )
        """
    )
    # Seed from faces table if empty embeddings for a face
    cur.execute(
        """
        INSERT INTO face_embeddings (face_id, embedding, created_at)
        SELECT id, embedding, datetime('now') FROM faces
        WHERE embedding IS NOT NULL AND id NOT IN (
            SELECT face_id FROM face_embeddings
        )
        """
    )
    conn.commit()


def ensure_meetings_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            audio_path TEXT,
            transcript TEXT,
            summary TEXT,
            FOREIGN KEY(person_id) REFERENCES faces(id)
        )
        """
    )
    conn.commit()


def ensure_all_schemas(conn: sqlite3.Connection) -> None:
    ensure_faces_schema(conn)
    ensure_face_embeddings_schema(conn)
    ensure_meetings_schema(conn)

# --- Embedding pruning ------------------------------------------------------

def prune_embeddings_if_needed(conn: sqlite3.Connection, face_id: int, max_count: int = MAX_TEMPLATES_PER_PERSON) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, quality, created_at FROM face_embeddings WHERE face_id = ?",
        (face_id,),
    )
    rows = cur.fetchall()
    if len(rows) <= max_count:
        return

    def sort_key(r):
        rid, q, ts = r
        qv = -1.0 if q is None else float(q)
        return (qv, ts or "")

    rows_sorted = sorted(rows, key=sort_key)
    to_remove = rows_sorted[: max(0, len(rows_sorted) - max_count)]
    ids = [r[0] for r in to_remove]
    if ids:
        cur.executemany("DELETE FROM face_embeddings WHERE id = ?", [(i,) for i in ids])
        conn.commit()

# --- Connection helper ------------------------------------------------------

def open_db(path: Optional[str] = None) -> sqlite3.Connection:
    """Open database, ensuring schema exists. If path omitted, use standard DB_PATH.
    """
    db_path = path or DB_PATH
    conn = sqlite3.connect(db_path)
    ensure_all_schemas(conn)
    return conn


def get_latest_finished_meeting(conn: sqlite3.Connection, person_id: int):
    """Return the most recent finished meeting row for a person.

    Returns a tuple: (meeting_id, ended_at, transcript, summary) or None.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ended_at, COALESCE(transcript,''), COALESCE(summary,'')
        FROM meetings
        WHERE person_id = ? AND ended_at IS NOT NULL
        ORDER BY datetime(ended_at) DESC, id DESC
        LIMIT 1
        """,
        (int(person_id),),
    )
    row = cur.fetchone()
    return row


def get_latest_finished_meeting_summary(conn: sqlite3.Connection, person_id: int) -> str:
    row = get_latest_finished_meeting(conn, person_id)
    if not row:
        return ""
    _mid, _ended_at, _transcript, summary = row
    return summary or ""

# Convenience: expose path
__all__ = [
    "DB_PATH",
    "open_db",
    "ensure_all_schemas",
    "prune_embeddings_if_needed",
    "MAX_TEMPLATES_PER_PERSON",
    "get_latest_finished_meeting",
    "get_latest_finished_meeting_summary",
]
