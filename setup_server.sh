#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# TrueVision Server Setup
# Target: Linux desktop/server (Ubuntu/Debian) with optional NVIDIA GPU
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║         TrueVision Server Setup                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: System packages ──────────────────────────────────────────────────

echo "── Step 1/6: System packages ──"
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        python3 python3-pip python3-venv python3-dev \
        make curl wget sqlite3 \
        libsndfile1 libsndfile1-dev \
        build-essential
elif command -v dnf &>/dev/null; then
    sudo dnf install -y \
        python3 python3-pip python3-devel \
        make curl wget sqlite \
        libsndfile libsndfile-devel \
        gcc gcc-c++
else
    echo "WARNING: Package manager not recognized (not apt or dnf). "
    echo "         Please install manually: python3, pip, venv, make, curl, sqlite3, libsndfile"
fi

# ── Step 2: Check NVIDIA/CUDA ────────────────────────────────────────────────

echo ""
echo "── Step 2/6: GPU check ──"
GPU_AVAILABLE=0
if command -v nvidia-smi &>/dev/null; then
    echo "NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null || true
    GPU_AVAILABLE=1

    if command -v nvcc &>/dev/null; then
        echo "CUDA compiler: $(nvcc --version | grep release | awk '{print $NF}')"
    else
        echo "WARNING: nvcc not found. CUDA toolkit may not be installed."
        echo "         faster-whisper will still work with CTranslate2's bundled CUDA libs,"
        echo "         but for best performance install the CUDA toolkit:"
        echo "         https://developer.nvidia.com/cuda-downloads"
    fi
else
    echo "No NVIDIA GPU detected — Whisper will run on CPU."
    echo "Set WHISPER_DEVICE=cpu in your environment."
fi

export WHISPER_DEVICE="${WHISPER_DEVICE:-auto}"

# ── Step 3: Python venv ──────────────────────────────────────────────────────

echo ""
echo "── Step 3/6: Python virtual environment ──"
if [[ -d "$VENV_DIR" ]]; then
    echo "Existing venv found at $VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
    echo "Created venv at $VENV_DIR"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip setuptools wheel -q

# ── Step 4: pip packages ─────────────────────────────────────────────────────

echo ""
echo "── Step 4/6: Python packages ──"

# Core server
pip install -q \
    "fastapi>=0.100" \
    "uvicorn[standard]>=0.20" \
    "websockets>=11.0" \
    "requests>=2.28"

# Audio / transcription
pip install -q \
    "numpy<2.0" \
    "soundfile>=0.12"

if [[ $GPU_AVAILABLE -eq 1 ]]; then
    echo "Installing faster-whisper with CUDA support..."
    pip install -q "faster-whisper>=1.0"
else
    echo "Installing faster-whisper (CPU mode)..."
    pip install -q "faster-whisper>=1.0"
fi

# mDNS discovery
pip install -q "zeroconf>=0.80"

echo "Python packages installed."

# ── Step 5: Ollama ───────────────────────────────────────────────────────────

echo ""
echo "── Step 5/6: Ollama (LLM summarization) ──"
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.1:8b}"

if command -v ollama &>/dev/null; then
    echo "Ollama already installed: $(ollama --version 2>/dev/null || echo 'unknown version')"
else
    echo "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
fi

echo "Pulling model: $OLLAMA_MODEL (this may take a while on first run)..."
ollama pull "$OLLAMA_MODEL" || echo "WARNING: Could not pull model. Ensure ollama service is running."

# ── Step 6: Verification ────────────────────────────────────────────────────

echo ""
echo "── Step 6/6: Verification ──"

"$VENV_DIR/bin/python3" -c "
import sys
ok = True

def check(name, mod):
    global ok
    try:
        __import__(mod)
        print(f'  ✓ {name}')
    except ImportError as e:
        print(f'  ✗ {name}: {e}')
        ok = False

check('FastAPI',        'fastapi')
check('Uvicorn',        'uvicorn')
check('WebSockets',     'websockets')
check('Requests',       'requests')
check('NumPy',          'numpy')
check('SoundFile',      'soundfile')
check('faster-whisper', 'faster_whisper')
check('Zeroconf',       'zeroconf')

# GPU check
try:
    import torch
    if torch.cuda.is_available():
        print(f'  ✓ CUDA: {torch.cuda.get_device_name(0)}')
    else:
        print(f'  ⚠ torch installed but no CUDA — will use CPU')
except ImportError:
    print(f'  ⚠ torch not installed — GPU check skipped (faster-whisper has its own CUDA)')

if ok:
    print()
    print('All dependencies verified.')
else:
    print()
    print('Some dependencies missing — see above.')
    sys.exit(1)
"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                        ║"
echo "║                                                         ║"
echo "║  Start the server:                                      ║"
echo "║    make run-server                                      ║"
echo "║                                                         ║"
echo "║  Or with auto-reload (development):                     ║"
echo "║    make run-server-dev                                   ║"
echo "║                                                         ║"
echo "║  Environment variables:                                  ║"
echo "║    WHISPER_MODEL     (default: small)                    ║"
echo "║    WHISPER_DEVICE    (default: auto)                     ║"
echo "║    OLLAMA_MODEL      (default: llama3.1:8b)              ║"
echo "║    TRUEVISION_SERVER_PORT (default: 8008)                ║"
echo "╚══════════════════════════════════════════════════════════╝"
