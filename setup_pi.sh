#!/usr/bin/env bash
# TrueVision Raspberry Pi 5 Setup Script
# Run this from the repository root on a fresh Pi OS (Bookworm 64-bit) image.
# Usage: bash setup_pi.sh
# Do NOT run with sudo directly — the script will call sudo internally where needed.

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ ${EUID:-$(id -u)} -eq 0 ]] && die "Do not run this script as root. Run as your normal Pi user; sudo will be called when needed."

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Repository root: ${REPO_DIR}"
cd "${REPO_DIR}"

# Verify we look like a Raspberry Pi
if ! grep -qi "raspberry pi" /proc/cpuinfo 2>/dev/null && \
   ! grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    warn "Could not confirm this is a Raspberry Pi. Proceeding anyway."
fi

# ── Step 1: System package update ─────────────────────────────────────────────
info "=== Step 1/6: Updating system packages ==="
sudo apt-get update -y
sudo apt-get full-upgrade -y
success "System packages updated."

# ── Step 2: Install apt dependencies ──────────────────────────────────────────
info "=== Step 2/6: Installing apt dependencies ==="

# Core Python tooling
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    make \
    bzip2 \
    wget \
    git

# OpenCV with GStreamer (from apt — avoids heavy pip build, keeps GStreamer support)
# python3-picamera2 provides the Picamera2 fallback
# NOTE: python3-dlib was removed from Debian Trixie repos; we build it via pip below.
sudo apt-get install -y \
    python3-opencv \
    python3-picamera2 \
    || warn "One or more camera/vision apt packages failed — see output above."

# dlib build dependencies (required since python3-dlib is absent from Trixie)
# This lets 'pip install dlib' compile from source (~10-20 min on Pi 5)
sudo apt-get install -y \
    cmake \
    build-essential \
    python3-dev \
    libboost-dev \
    libboost-python-dev \
    libboost-thread-dev \
    libx11-dev

# Camera and GStreamer stack
sudo apt-get install -y \
    libcamera-apps \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    || true  # package names vary by Pi OS version; non-fatal

# Try the newer rpicam-apps name too (Pi OS Bookworm)
sudo apt-get install -y rpicam-apps 2>/dev/null || true

# Optional GStreamer libcamera plugin (may not exist on all images)
sudo apt-get install -y gstreamer1.0-libcamera 2>/dev/null || true

# Math / BLAS libs required by numpy / scipy
# Note: libatlas-base-dev was removed from Debian Trixie; libopenblas-dev replaces it.
sudo apt-get install -y \
    libopenblas-dev \
    libopenblas0 \
    liblapack3 \
    liblapack-dev

# Audio: PortAudio dev headers and libsndfile (for soundfile)
# PortAudio is kept for potential future use; libsndfile is required by soundfile
sudo apt-get install -y \
    portaudio19-dev \
    libportaudio2 \
    libsndfile1 \
    libsndfile1-dev

# Useful extras
sudo apt-get install -y \
    sqlite3 \
    curl

success "apt dependencies installed."

# ── Step 3: Create Python virtual environment ──────────────────────────────────
info "=== Step 3/6: Setting up Python virtual environment ==="

VENV_DIR="${REPO_DIR}/.venv"
# Always (re)create the venv to guarantee --system-site-packages is set.
# This flag is required so the venv can see apt-installed python3-opencv and
# python3-picamera2. A venv created without it will cause pip to target the
# system Python and hit the "externally managed environment" error on Trixie.
if [[ -d "${VENV_DIR}" ]]; then
    warn ".venv already exists — removing and recreating to ensure correct flags..."
    rm -rf "${VENV_DIR}"
fi
python3 -m venv "${VENV_DIR}" --system-site-packages
success "Virtual environment created at .venv (--system-site-packages)"

# Activate for the remainder of this script
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
info "Activated .venv (python: $(which python3))"

pip install --upgrade pip setuptools wheel

# ── Step 4: Install Python pip packages ───────────────────────────────────────
info "=== Step 4/6: Installing Python packages via pip ==="

# Core project requirements (excluding opencv/dlib — those come from apt)
# We install everything in requirements.txt and then patch out the apt-only ones.
pip install \
    "numpy<2.0" \
    Pillow \
    soundfile \
    pyserial

# Build dlib from source (no apt package on Trixie — takes ~10-20 min on Pi 5)
info "Building dlib from source (this takes 10-20 minutes — please wait)..."
pip install dlib

# Transcription (Whisper via faster-whisper + CTranslate2)
# This is a large download (~500MB to 1GB including models); be patient.
info "Installing faster-whisper (large download — may take several minutes)..."
pip install faster-whisper

# Summarization service client (needed by Pi to contact the remote Ollama service)
pip install requests

# Server offload support (needed by Pi to discover and stream audio to TrueVision server)
pip install websocket-client zeroconf

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

# ── Step 6: Configure UART for ESP32 audio ────────────────────────────────────
info "=== Step 6/6: Configuring UART for ESP32 audio ==="

# Detect Pi model — Pi 5 uses the RP1 I/O chip and needs different UART setup.
PI_MODEL="$(cat /proc/device-tree/model 2>/dev/null || true)"
if echo "${PI_MODEL}" | grep -q "Pi 5"; then
    IS_PI5=1
    info "Detected Raspberry Pi 5 — applying Pi 5 UART configuration."
else
    IS_PI5=0
fi

# Enable serial hardware and disable the login console via raspi-config
if command -v raspi-config &>/dev/null; then
    sudo raspi-config nonint do_serial_hw 0   # Enable serial hardware
    sudo raspi-config nonint do_serial_cons 1 # Disable login shell over serial
    success "raspi-config: serial hardware enabled, serial console disabled."
else
    warn "raspi-config not found — skipping automated serial config. Configure manually:"
    warn "  sudo raspi-config → Interface Options → Serial Port"
    warn "  'Login shell over serial?' → No"
    warn "  'Enable serial hardware?' → Yes"
fi

# Pi 5 specific: the RP1 PL011 UART needs dtoverlay=uart0 to route it to
# GPIO14/15. Without this the pins are not connected to /dev/ttyAMA0 properly.
# Pi 5 config lives at /boot/firmware/config.txt (not /boot/config.txt).
if [[ ${IS_PI5} -eq 1 ]]; then
    CFG="/boot/firmware/config.txt"
    if [[ -f "${CFG}" ]]; then
        if ! grep -q "dtoverlay=uart0" "${CFG}"; then
            echo "dtoverlay=uart0" | sudo tee -a "${CFG}" >/dev/null
            success "Pi 5: added dtoverlay=uart0 to ${CFG}"
        else
            success "Pi 5: dtoverlay=uart0 already present in ${CFG}"
        fi
        if ! grep -q "enable_uart=1" "${CFG}"; then
            echo "enable_uart=1" | sudo tee -a "${CFG}" >/dev/null
            success "Pi 5: added enable_uart=1 to ${CFG}"
        fi
    else
        warn "${CFG} not found — add these lines manually:\n  enable_uart=1\n  dtoverlay=uart0"
    fi
fi

# Disable serial-getty services so they don't hold the port
for svc in serial-getty@ttyS0.service serial-getty@ttyAMA0.service serial-getty@ttyAMA10.service; do
    if systemctl list-unit-files | grep -q "^${svc}"; then
        sudo systemctl disable --now "${svc}" 2>/dev/null || true
    fi
done
success "serial-getty services disabled."

# Add user to dialout and tty groups
sudo usermod -aG dialout,tty "${USER}"
success "Added ${USER} to dialout and tty groups."

# Install a udev rule to ensure ttyS0/ttyAMA0 are accessible without sudo
UDEV_RULE="/etc/udev/rules.d/99-truevision-serial.rules"
if [[ ! -f "${UDEV_RULE}" ]]; then
    sudo tee "${UDEV_RULE}" >/dev/null <<'EOF'
SUBSYSTEM=="tty", KERNEL=="ttyS0",   GROUP="dialout", MODE="0660", OPTIONS+="last_rule"
SUBSYSTEM=="tty", KERNEL=="ttyAMA0", GROUP="dialout", MODE="0660", OPTIONS+="last_rule"
SUBSYSTEM=="tty", KERNEL=="ttyAMA10",GROUP="dialout", MODE="0660", OPTIONS+="last_rule"
EOF
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=ttyS0  2>/dev/null || true
    sudo udevadm trigger --name-match=ttyAMA0 2>/dev/null || true
    success "udev serial permission rules installed."
else
    success "udev serial rules already present."
fi

# ── Create required runtime directories ───────────────────────────────────────
info "Creating runtime directories..."
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
    ("serial",     "pyserial"),
    ("soundfile",  "soundfile"),
]

for mod, label in checks:
    try:
        __import__(mod)
        print(f"  [OK] {label}")
    except ImportError as e:
        print(f"  [FAIL] {label}: {e}", file=sys.stderr)
        failures.append(label)

optional = [
    ("faster_whisper", "faster-whisper"),
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
echo -e "${GREEN}║           TrueVision Setup Complete                         ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "IMPORTANT — A reboot is required to apply:"
echo "  • UART serial configuration"
echo "  • Group membership (dialout, tty)"
echo ""
echo "After rebooting, start the app with:"
echo "  cd ${REPO_DIR}"
echo "  source .venv/bin/activate"
echo "  make run"
echo ""
echo "Or without audio (lighter on resources):"
echo "  make run-face"
echo ""
echo "To add faces to the recognition database:"
echo "  python -m facial_recognition.add_face"
echo ""
echo -e "${YELLOW}Rebooting in 10 seconds. Press Ctrl+C to skip the reboot.${NC}"
sleep 10
sudo reboot
