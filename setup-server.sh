#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SERVER_VENV_DIR:-$ROOT_DIR/.venv-server}"
PYTHON_BIN="${SERVER_PYTHON_BIN:-python3}"

run_with_privileges() {
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

install_system_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "[setup-server] Installing Linux system packages"
    run_with_privileges apt-get update
    run_with_privileges apt-get install -y \
      python3-venv \
      python3-pip \
      ffmpeg \
      build-essential \
      cmake
  elif command -v brew >/dev/null 2>&1; then
    echo "[setup-server] Installing Homebrew packages"
    brew install ffmpeg cmake
  else
    echo "[setup-server] No supported system package manager detected"
    echo "[setup-server] Ensure ffmpeg is installed before running the DGX server"
  fi
}

install_system_packages

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install \
  numpy \
  opencv-python \
  requests \
  fastapi \
  pydantic \
  "uvicorn[standard]" \
  faster-whisper

if "$VENV_DIR/bin/python" -m pip install face-recognition; then
  echo "[setup-server] Optional face_recognition support installed"
else
  echo "[setup-server] Optional face_recognition install failed; server will still run without it"
fi

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
]

missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)

if missing:
    raise SystemExit(f"Missing server dependencies after setup: {', '.join(missing)}")

try:
    importlib.import_module("face_recognition")
    print("[setup-server] Optional face_recognition support is available")
except Exception:
    print("[setup-server] Optional face_recognition support is unavailable")

print("[setup-server] Core server dependencies are ready")
PY

echo "[setup-server] Virtual environment ready at $VENV_DIR"
echo "[setup-server] Run 'make run-server' to start the DGX server"