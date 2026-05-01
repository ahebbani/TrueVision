#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SERVER_VENV_DIR:-$ROOT_DIR/.venv-server}"
PYTHON_BIN="${SERVER_PYTHON_BIN:-python3}"

echo "[setup-server] Creating virtual environment at $VENV_DIR"

"$PYTHON_BIN" -m venv --clear "$VENV_DIR"

# setuptools<81 is required so that face_recognition_models can import pkg_resources
"$VENV_DIR/bin/python" -m pip install --upgrade pip "setuptools<81" wheel

echo "[setup-server] Installing core dependencies"
"$VENV_DIR/bin/python" -m pip install \
  numpy \
  opencv-python \
  requests \
  fastapi \
  pydantic \
  "uvicorn[standard]" \
  faster-whisper

echo "[setup-server] Installing face recognition"
# face_recognition_models must come from git; the PyPI wheel omits the model data
"$VENV_DIR/bin/python" -m pip install \
  "git+https://github.com/ageitgey/face_recognition_models" \
  face-recognition

echo "[setup-server] Verifying imports"
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
    except BaseException as exc:
        missing.append(f"{name} ({exc})")

if missing:
    raise SystemExit("Missing server dependencies:\n  " + "\n  ".join(missing))

print("[setup-server] All dependencies verified")
PY

echo "[setup-server] Virtual environment ready at $VENV_DIR"
echo "[setup-server] Run 'make run-server' to start the DGX server"
