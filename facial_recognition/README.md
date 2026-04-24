facial_recognition
====================

Purpose
- Face capture, template management, and recognition utilities used by `main.py`.

Key modules
- `recognizer.py` — Core dlib-based recognizer. Exposes `RecognizerConfig` and
  `Recognizer` classes. Responsibilities:
  - Detect faces in frames using HOG or CNN detectors (auto-selects based on platform).
  - Compute dlib face embeddings and match against embeddings in the DB.
  - Provide `detect_and_recognize(conn, frame)` returning `FaceInfo` (rect, embedding,
    person id, name, quality, distance).
  - Manage template addition policy: bootstrap vs steady-state thresholds,
    cooldowns, and diversity checks before inserting new templates.
- `camera.py` — Helper to open a camera device/frame capture with OpenCV.
- `add_face.py` — CLI helper to add a person with an initial template to the DB.
- `manage_embeddings.py` — Utilities for pruning, exporting, or migrating embeddings.

Models
- `models/fetch_models.py` — helper to download the required dlib model files
  (`shape_predictor_68_face_landmarks.dat`, `dlib_face_recognition_resnet_model_v1.dat`,
  and optional CNN detector). The `Recognizer` expects these files in a `models` dir.

Integration
- `main.py` constructs `RecognizerConfig` and instantiates `Recognizer` at runtime.
- The recognizer uses the DB (via `data_access`) to fetch stored templates and updates
  seen counts / last_seen timestamps after matches.
