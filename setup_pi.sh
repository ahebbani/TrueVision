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
info "=== Step 1/7: Updating system packages ==="
sudo apt-get update -y
sudo apt-get full-upgrade -y
success "System packages updated."

# ── Step 2: Install apt dependencies ──────────────────────────────────────────
info "=== Step 2/7: Installing apt dependencies ==="

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
# python3-dlib avoids compiling dlib from source on the Pi (hours of build time)
# python3-picamera2 provides the Picamera2 fallback
sudo apt-get install -y \
    python3-opencv \
    python3-dlib \
    python3-picamera2 \
    || warn "One or more camera/vision apt packages failed — see output above."

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
sudo apt-get install -y \
    libatlas-base-dev \
    libopenblas0 \
    liblapack3

# Audio: PortAudio dev headers (needed to build the sounddevice pip wheel)
# and libsndfile (for soundfile)
sudo apt-get install -y \
    portaudio19-dev \
    libportaudio2 \
    libsndfile1 \
    libsndfile1-dev

# TTS: espeak-ng (used by CaptionSpeaker when pyttsx3 is not installed)
sudo apt-get install -y espeak-ng

# I2C tools (for OLED display — smbus2 and luma.oled depend on kernel i2c support)
sudo apt-get install -y \
    i2c-tools \
    python3-smbus

# Useful extras
sudo apt-get install -y \
    sqlite3 \
    curl

success "apt dependencies installed."

# ── Step 3: Create Python virtual environment ──────────────────────────────────
info "=== Step 3/7: Setting up Python virtual environment ==="

VENV_DIR="${REPO_DIR}/.venv"
if [[ -d "${VENV_DIR}" ]]; then
    warn ".venv already exists — skipping creation. Delete it manually and re-run to start fresh."
else
    # --system-site-packages is critical: lets the venv see apt-installed
    # python3-opencv, python3-dlib, and python3-picamera2
    python3 -m venv "${VENV_DIR}" --system-site-packages
    success "Virtual environment created at .venv"
fi

# Activate for the remainder of this script
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
info "Activated .venv (python: $(which python3))"

pip install --upgrade pip setuptools wheel

# ── Step 4: Install Python pip packages ───────────────────────────────────────
info "=== Step 4/7: Installing Python packages via pip ==="

# Core project requirements (excluding opencv/dlib — those come from apt)
# We install everything in requirements.txt and then patch out the apt-only ones.
pip install \
    "numpy<2.0" \
    Pillow \
    "luma.oled" \
    sounddevice \
    soundfile \
    pyserial

# Transcription (Whisper via faster-whisper + CTranslate2)
# This is a large download (~500MB to 1GB including models); be patient.
info "Installing faster-whisper (large download — may take several minutes)..."
pip install faster-whisper

# TTS backend (pyttsx3 is preferred by CaptionSpeaker; espeak-ng is the system fallback)
pip install pyttsx3

# Summarization service client (needed by Pi to contact the remote Ollama service)
pip install requests

success "pip packages installed."

# ── Step 5: Download dlib model files ─────────────────────────────────────────
info "=== Step 5/7: Downloading dlib facial recognition models (~200MB) ==="

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
info "=== Step 6/7: Configuring UART for ESP32 audio ==="

# Enable serial hardware and disable the login console via raspi-config
# Noninteractive mode: 0 = disable, 1 = enable
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

# ── Step 7: Enable I2C for OLED display ───────────────────────────────────────
info "=== Step 7/7: Enabling I2C (OLED display) ==="

if command -v raspi-config &>/dev/null; then
    sudo raspi-config nonint do_i2c 0  # 0 = enable
    success "I2C enabled via raspi-config."
else
    warn "raspi-config not found — enable I2C manually:"
    warn "  sudo raspi-config → Interface Options → I2C → Yes"
fi

# Add user to i2c group if it exists
if getent group i2c &>/dev/null; then
    sudo usermod -aG i2c "${USER}"
    success "Added ${USER} to i2c group."
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
    ("sounddevice","sounddevice"),
    ("soundfile",  "soundfile"),
    ("luma.oled",  "luma.oled"),
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
    ("pyttsx3",        "pyttsx3"),
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
echo "  • I2C enable"
echo "  • Group membership (dialout, tty, i2c)"
echo ""
echo "After rebooting, start the app with:"
echo "  cd ${REPO_DIR}"
echo "  source .venv/bin/activate"
echo "  make run"
echo ""
echo "Or without audio (lighter on resources):"
echo "  make run-no-audio"
echo ""
echo "To add faces to the recognition database:"
echo "  python -m facial_recognition.add_face"
echo ""
echo -e "${YELLOW}Rebooting in 10 seconds. Press Ctrl+C to skip the reboot.${NC}"
sleep 10
sudo reboot
