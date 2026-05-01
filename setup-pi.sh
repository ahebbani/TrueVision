#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PI_VENV_DIR:-$ROOT_DIR/.venv-pi}"
PYTHON_BIN="${PI_PYTHON_BIN:-python3}"

run_with_privileges() {
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

install_system_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "[setup-pi] Installing Raspberry Pi system packages"
    run_with_privileges apt-get update
    run_with_privileges apt-get install -y \
      python3-venv \
      python3-pip \
      python3-opencv \
      python3-picamera2
  else
    echo "[setup-pi] apt-get not found; skipping Raspberry Pi OS packages"
    echo "[setup-pi] Ensure OpenCV and picamera2 are installed manually for your platform"
  fi
}

install_system_packages

"$PYTHON_BIN" -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install \
  numpy \
  requests \
  pyserial \
  websocket-client \
  fastapi \
  uvicorn

if ! "$VENV_DIR/bin/python" -c "import cv2" >/dev/null 2>&1; then
  echo "[setup-pi] OpenCV not found in system packages; installing pip wheel"
  "$VENV_DIR/bin/python" -m pip install opencv-python
fi

"$VENV_DIR/bin/python" - <<'PY'
import importlib

required = [
    "cv2",
    "numpy",
    "requests",
    "serial",
    "websocket",
    "fastapi",
    "uvicorn",
]

missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)

if missing:
    raise SystemExit(f"Missing Pi dependencies after setup: {', '.join(missing)}")

try:
    importlib.import_module("picamera2")
    print("[setup-pi] Optional picamera2 support is available")
except Exception:
    print("[setup-pi] Optional picamera2 support is unavailable; USB camera fallback will still work")

print("[setup-pi] Core Pi dependencies are ready")
PY

echo "[setup-pi] Virtual environment ready at $VENV_DIR"
echo "[setup-pi] Run 'make run' to start the Pi client"