#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SERVER_VENV_DIR:-$ROOT_DIR/.venv-server}"
PYTHON_BIN="${SERVER_PYTHON_BIN:-python3}"

echo "[setup-server] Creating virtual environment at $VENV_DIR"

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install \
  numpy \
  opencv-python \
  requests \
  fastapi \
  pydantic \
  "uvicorn[standard]" \
  faster-whisper \
  face-recognition-models \
  face-recognition

"$VENV_DIR/bin/python" - <<'PY'
import importlib

required = [
    "cv2",
    "numpy",
    "requests",
    "fastapi",
    "pydantic",
    "uvicorn",
    "faster_whisper",
    "face_recognition_models",
    "face_recognition",
]

missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)

if missing:
    raise SystemExit(f"Missing server dependencies after setup: {', '.join(missing)}")

print("[setup-server] Server dependencies are ready")
PY

echo "[setup-server] Virtual environment ready at $VENV_DIR"
echo "[setup-server] Run 'make run-server' to start the DGX server"