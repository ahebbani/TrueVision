data_access
=============

Purpose
- Centralized SQLite helpers used by both the Pi client and the server. Manages
  schema creation, convenience helpers for inserting faces and pruning embeddings,
  and provides a stable DB path used across the codebase.

Key module: `db.py`
- `DB_PATH` — default location for the SQLite file (inside `data_access/`).
- `open_db(path=None)` — opens the DB and ensures the required schemas exist.
- Schema helpers: `ensure_faces_schema()`, `ensure_face_embeddings_schema()`,
  `ensure_meetings_schema()`, and `ensure_all_schemas()`.
- Embedding management: `insert_face_with_template()`, `prune_embeddings_if_needed()`
  (keeps at most `MAX_TEMPLATES_PER_PERSON` per person).
- Convenience: `get_latest_finished_meeting()` and `get_latest_finished_meeting_summary()`.

Other module: `visualize_db.py`
- Utilities to inspect and visualize DB contents (developer tooling).

Integration points
- `facial_recognition.Recognizer` uses this DB to load templates and persist
  face_embeddings. `main.py` logs meetings into the `meetings` table.
