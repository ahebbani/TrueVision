"""Database access and schema management for TrueVision.

Provides helpers to open the SQLite database and ensure all tables exist.
Other modules should import from here instead of duplicating schema logic.
"""
from .db import (
    DB_PATH,
    open_db,
    ensure_all_schemas,
    insert_face_with_template,
    prune_embeddings_if_needed,
    MAX_TEMPLATES_PER_PERSON,
    get_latest_finished_meeting,
    get_latest_finished_meeting_summary,
)

__all__ = [
    "DB_PATH",
    "open_db",
    "ensure_all_schemas",
    "insert_face_with_template",
    "prune_embeddings_if_needed",
    "MAX_TEMPLATES_PER_PERSON",
    "get_latest_finished_meeting",
    "get_latest_finished_meeting_summary",
]
