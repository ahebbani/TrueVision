#!/usr/bin/env bash
# TrueVision macOS Development Setup Script
# Run this from the repository root on a Mac with Homebrew available.
# Usage: bash setup_mac.sh
#
# This sets up the project for local development/testing on macOS.
# The default webcam (AVFoundation) and built-in microphone will be used.
# ESP32 serial audio and OLED display are Pi-only and will be gracefully skipped.

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || die "This script is for macOS only. Use setup_pi.sh on Raspberry Pi."

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Repository root: ${REPO_DIR}"
cd "${REPO_DIR}"

# ── Step 1: Install Homebrew (if missing) ─────────────────────────────────────
info "=== Step 1/6: Checking Homebrew ==="
if ! command -v brew &>/dev/null; then
    info "Homebrew not found — installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon Macs
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
    success "Homebrew installed."
else
    success "Homebrew found at $(which brew)"
fi

# ── Step 2: Install system dependencies via Homebrew ──────────────────────────
info "=== Step 2/6: Installing system dependencies ==="

brew install cmake || true
brew install python@3 || true
brew install portaudio || true
brew install libsndfile || true
brew install espeak || true
brew install wget || true

success "System dependencies installed."

# ── Step 3: Create Python virtual environment ─────────────────────────────────
info "=== Step 3/6: Setting up Python virtual environment ==="

VENV_DIR="${REPO_DIR}/.venv"

# Deactivate any active conda / virtualenv so we start clean
if [[ -n "${CONDA_PREFIX:-}" ]]; then
    warn "Conda environment detected (${CONDA_PREFIX}). Deactivating for .venv setup."
    { conda deactivate 2>/dev/null || true; }
fi
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    warn "Existing virtualenv active (${VIRTUAL_ENV}). Deactivating."
    deactivate 2>/dev/null || true
fi

# Detect Python — prefer brew python3 over system
if [[ -f /opt/homebrew/bin/python3 ]]; then
    PY3="/opt/homebrew/bin/python3"
elif command -v python3 &>/dev/null; then
    PY3="python3"
else
    die "Python 3 not found. Install via: brew install python@3"
fi

info "Using Python: $(${PY3} --version 2>&1) at ${PY3}"

if [[ -d "${VENV_DIR}" ]]; then
    warn ".venv already exists — removing and recreating to ensure correct setup..."
    rm -rf "${VENV_DIR}"
fi
${PY3} -m venv "${VENV_DIR}"
success "Virtual environment created at .venv"

# Activate for the remainder of this script
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
info "Activated .venv (python: $(which python3))"

pip install --upgrade pip setuptools wheel

# ── Step 4: Install Python pip packages ───────────────────────────────────────
info "=== Step 4/6: Installing Python packages via pip ==="

# Core packages
pip install \
    "numpy<2.0" \
    Pillow \
    opencv-python \
    sounddevice \
    soundfile \
    pyserial \
    requests

# dlib — on macOS with Apple Silicon this may need cmake (already installed above)
info "Building dlib (may take several minutes to compile)..."
pip install dlib

# Whisper transcription — large download (~500MB+ including models)
info "Installing faster-whisper (large download — may take several minutes)..."
pip install faster-whisper

# TTS (optional — may have limited voice support on macOS)
pip install pyttsx3 || warn "pyttsx3 install failed — spoken captions will be unavailable."

# Summarization service (FastAPI + uvicorn, for running the summarizer locally)
pip install fastapi uvicorn

success "pip packages installed."

# ── Step 5: Download dlib model files ─────────────────────────────────────────
info "=== Step 5/6: Downloading dlib facial recognition models (~200MB) ==="

MODELS_DIR="${REPO_DIR}/facial_recognition/models"
mkdir -p "${MODELS_DIR}"

download_model() {
    local filename="$1"
    local url="$2"
    local dest="${MODELS_DIR}/${filename}"
    local bz2="${dest}.bz2"

    if [[ -f "${dest}" ]]; then
        success "Model already present: ${filename}"
        return
    fi

    info "Downloading ${filename}..."
    wget -q --show-progress -O "${bz2}" "${url}"
    info "Extracting ${filename}..."
    bzip2 -dk "${bz2}"
    rm -f "${bz2}"
    success "Model ready: ${filename}"
}

download_model \
    "shape_predictor_68_face_landmarks.dat" \
    "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2"

download_model \
    "dlib_face_recognition_resnet_model_v1.dat" \
    "http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2"

# ── Step 6: Create runtime directories & verify ──────────────────────────────
info "=== Step 6/6: Final setup ==="

mkdir -p "${REPO_DIR}/data/recordings"
mkdir -p "${REPO_DIR}/facial_recognition/models"
success "Runtime directories ready."

# ── Smoke-test key Python imports ─────────────────────────────────────────────
info "Running import smoke test..."

python3 - <<'PYCHECK'
import sys
failures = []

checks = [
    ("cv2",        "OpenCV"),
    ("dlib",       "dlib"),
    ("numpy",      "NumPy"),
    ("PIL",        "Pillow"),
    ("sounddevice","sounddevice"),
    ("soundfile",  "soundfile"),
    ("serial",     "pyserial"),
    ("requests",   "requests"),
]

for mod, label in checks:
    try:
        __import__(mod)
        print(f"  [OK] {label}")
    except ImportError as e:
        print(f"  [FAIL] {label}: {e}", file=sys.stderr)
        failures.append(label)

optional = [
    ("faster_whisper",  "faster-whisper"),
    ("pyttsx3",         "pyttsx3"),
    ("fastapi",         "fastapi"),
    ("uvicorn",         "uvicorn"),
]
for mod, label in optional:
    try:
        __import__(mod)
        print(f"  [OK] {label} (optional)")
    except ImportError:
        print(f"  [--] {label} not importable (optional — non-fatal)")

if failures:
    print(f"\nFailed required imports: {', '.join(failures)}", file=sys.stderr)
    sys.exit(1)
PYCHECK

success "All required imports verified."

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       TrueVision macOS Dev Setup Complete                   ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "To start the app:"
echo "  cd ${REPO_DIR}"
echo "  source .venv/bin/activate"
echo "  make run-face          # Face recognition only (no audio)"
echo "  make run               # Full system (webcam + microphone)"
echo "  make run-summarizer    # Summarization service (needs Ollama)"
echo ""
echo "To add faces to the recognition database:"
echo "  python -m facial_recognition.add_face"
echo ""
echo -e "${YELLOW}Notes for macOS:${NC}"
echo "  • Camera uses the built-in webcam via AVFoundation"
echo "  • Microphone uses the default audio input via sounddevice"
echo "  • ESP32 serial audio is Pi-only — auto mode falls back to mic"
echo "  • OLED display is Pi-only — code uses a dummy fallback"
echo "  • macOS may prompt for camera/microphone permissions on first run"
echo ""
