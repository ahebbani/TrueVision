# Raspberry Pi 5 setup (Debian Trixie/Bookworm 64-bit)

This project uses Picamera2, OpenCV, and dlib with a Raspberry Pi Camera Module on a Pi 5.
The camera backend is Picamera2 exclusively.

## 1) Update OS and enable camera

- Make sure your Pi is up to date and the camera works with libcamera.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

- Test the camera using the official apps (you should see a preview window):

```bash
sudo apt install -y libcamera-apps
# Note: newer Raspberry Pi OS renamed libcamera-* tools to rpicam-*
rpicam-hello -t 2000
# If the older name exists on your image, this also works:
# libcamera-hello -t 2000
```

If this fails, fix the camera stack first (cable seated, correct connector, and OS supports Camera Module 3 via libcamera).

## 2) Install system packages (recommended)

Using apt packages avoids heavy builds on the Pi and ensures GStreamer support for OpenCV.

```bash
sudo apt install -y \
  python3 \
  python3-pip python3-venv \
  python3-opencv \
  python3-dlib \
  python3-picamera2 \
  libatlas3-base libopenblas0 liblapack3 \
  libcamera-apps \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-libcamera || true
```

Notes:
- `python3-opencv` from apt is built with GStreamer, which lets OpenCV use the `libcamerasrc` pipeline.
- `python3-dlib` saves you from compiling `dlib` on the Pi.
- `python3-picamera2` gives a reliable fallback.  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \

- `gstreamer1.0-libcamera` may already be present or have a slightly different package name depending on your image; it's optional.

You can work with the system Python directly. If you prefer a virtual environment, create one that can see system packages so OpenCV/dlib remain available:

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip
# (No need to pip install opencv or dlib; they come from apt.)
```

Note: `.venv` is a hidden directory (name starts with a dot). Use `ls -la` to see it, or just activate it with `source .venv/bin/activate`.

Important: On the Pi, don’t run `pip install -r requirements.txt` as-is; it will try to install `opencv-python`/`dlib` wheels and may conflict with the apt versions. Use the apt packages above and only pip-install extra project-specific libs if you add any later.

## 2.5) Enable UART (/dev/serial0) for ESP32 audio

If you’re using the ESP32 mic over UART, the Pi side reads from `/dev/serial0` (which typically points at `/dev/ttyS0` on a Pi 4).

### A) Enable serial hardware and disable the serial login console

Run:

```bash
sudo raspi-config
```

Then:
- Interface Options → Serial Port
- “Login shell over serial?” → **No**
- “Enable serial hardware?” → **Yes**

Reboot when prompted.

### B) Fix permissions so you don’t need sudo

1) Add your user to the serial groups and reboot (logout/login is not always enough):

```bash
sudo usermod -aG dialout,tty $USER
sudo reboot
```

2) Verify `/dev/serial0` and the underlying device:

```bash
ls -l /dev/serial0
readlink -f /dev/serial0
ls -l /dev/ttyS0 /dev/ttyAMA0 2>/dev/null
```

Expected on a healthy setup is usually `crw-rw---- root dialout ... /dev/ttyS0`.

If you see `crw------- root tty ... /dev/ttyS0`, the UART is still configured as a console or another rule is forcing root-only mode.

3) Ensure `serial-getty` is not holding the port:

```bash
sudo systemctl disable --now serial-getty@ttyS0.service
sudo systemctl disable --now serial-getty@ttyAMA0.service
```

4) If `/dev/ttyS0` is still root-only, add a udev rule to force dialout + mode 0660:

```bash
sudo tee /etc/udev/rules.d/99-zz-serial-permissions.rules >/dev/null <<'EOF'
SUBSYSTEM=="tty", KERNEL=="ttyS0", GROUP="dialout", MODE="0660", OPTIONS+="last_rule"
SUBSYSTEM=="tty", KERNEL=="ttyAMA0", GROUP="dialout", MODE="0660", OPTIONS+="last_rule"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=ttyS0
sudo reboot
```

5) Quick verify (no sudo):

```bash
python3 -c "import serial; s=serial.Serial('/dev/serial0', 921600, timeout=1); print('opened'); s.close()"
```

If it still fails, check whether the kernel is using ttyS0 as an active console:

```bash
cat /sys/class/tty/console/active
cat /proc/cmdline
```

If either mentions `ttyS0`/`serial0`, remove the `console=...` serial token (or re-run `raspi-config` as above) and reboot.

## 3) Get the dlib model files

Place the model files in `facial_recognition/models/`:

```bash
cd ~/TrueVision/facial_recognition
mkdir -p models
cd models
# 68-point landmark predictor (~100MB)
wget http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2
bunzip2 shape_predictor_68_face_landmarks.dat.bz2
# Face embedding model (~100MB)
wget http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2
bunzip2 dlib_face_recognition_resnet_model_v1.dat.bz2
# Optional CNN face detector (slower on CPU, optional)
# wget http://dlib.net/files/mmod_human_face_detector.dat.bz2
# bunzip2 mmod_human_face_detector.dat.bz2
```

If you add the CNN detector file, you can run with `FACE_DETECTOR=cnn` to prefer it.

## 4) Quick camera sanity check

Verify Picamera2 can open the camera:

```bash
python3 - <<'PY'
from picamera2 import Picamera2
import cv2
picam2 = Picamera2()
picam2.configure(picam2.create_preview_configuration(main={"size": (640,480), "format": "RGB888"}))
picam2.start()
arr = picam2.capture_array()
print('Picamera2 frame:', arr.shape)
picam2.stop(); picam2.close()
PY
```

If this fails, make sure `rpicam-hello` works and libcamera is functioning correctly.

## 5) Enroll faces (one-time)

Run the enrollment script and press `s` to save a face when detected:

```bash
cd ~/TrueVision/facial_recognition
python3 add_face.py
```

This creates `database/faces.db` and stores your embeddings. Repeat to add more people.

Tip: good, front-lit, non-blurry shots improve recognition. The app will later add more templates over time for diversity.

## 6) Run recognition

```bash
cd ~/TrueVision/facial_recognition
# Optional: pick detector; default is HOG. If you downloaded the CNN model, you can use:
# FACE_DETECTOR=cnn python3 main.py
python3 main.py
```

Press `q` to quit.

## 7) Headless notes

- `cv2.imshow` needs a display. If you're headless, either use VNC/desktop, or run with a virtual display (e.g., `xvfb-run -a python3 main.py`) or temporarily comment out the imshow/keypress lines.
- If you are SSH'd into the Pi but want the window to appear on the Pi's attached monitor (desktop session), you must point your SSH session at the desktop display:

```bash
# In your SSH session
export DISPLAY=:0
export XAUTHORITY=/home/$USER/.Xauthority
python3 main.py
```

If that still prints a display connection error, the Pi desktop may not be logged in (no running GUI session), or X authority may not allow your SSH session. In that case, use VNC, attach a keyboard temporarily, or run using X11 forwarding to your laptop.

- To show the window on your laptop instead (X11 forwarding), install an X server on your laptop (e.g., XQuartz on macOS), then connect with trusted forwarding and run normally:

```bash
ssh -Y adity@raspberrypi.local
cd ~/Files/TrueVision
python3 main.py
```
- Performance: lower the camera resolution. In code we default to 640x480. You can change the `preferred_width/height` in `open_camera()` to 320x240 for a big CPU win.

## 8) Troubleshooting

- Camera won't open: make sure `rpicam-hello` (or `libcamera-hello` on older images) works first. The program uses Picamera2 exclusively.
- Import errors for `cv2`/`dlib`: ensure you’re using the system Python, or a venv created with `--system-site-packages` so it can see `python3-opencv` and `python3-dlib` from apt.
- "No display" errors: you’re headless; see notes above.
- Slow performance on CNN detector: use default HOG detector, reduce resolution, and avoid full-screen windows.

## 9) Start on boot (systemd)

The most reliable way to run on boot is a `systemd` service that executes `make run-face`.

### Option A (recommended): install the service using the repo script

From the repo root on the Pi:

```bash
chmod +x scripts/install_truevision_systemd.sh
sudo ./scripts/install_truevision_systemd.sh --user pi

# Check status / logs
systemctl status truevision --no-pager
journalctl -u truevision -f
```

Notes:
- If you created a venv at `.venv/`, the installer will automatically run using `.venv/bin/python3`.
- If you’re headless (no desktop/HDMI), OpenCV’s `cv2.imshow()` can fail. In that case install a virtual display and install the service with `--xvfb`:

```bash
sudo apt install -y xvfb
sudo ./scripts/install_truevision_systemd.sh --user pi --xvfb
```

### Option B: create the service manually

1) Create the unit file:

```bash
sudo nano /etc/systemd/system/truevision.service
```

2) Paste (edit paths/user to match your Pi):

```ini
[Unit]
Description=TrueVision (make run-face)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Files/TrueVision
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/make -C /home/pi/Files/TrueVision run-face
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

3) Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable truevision
sudo systemctl start truevision

systemctl status truevision --no-pager
journalctl -u truevision -f
```

### Fix: venv won’t create or activate

Common causes and fixes:

1) Missing venv module

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
```

2) ensurepip error during venv creation

If you see an error referencing `ensurepip`, install `python3-venv` (above) and retry. You can also try:

```bash
python3 -m ensurepip --upgrade || true
python3 -m venv .venv --system-site-packages
```

3) Permission issues in the repo folder

```bash
pwd  # should be your TrueVision repo
ls -ld .
id
# If needed (replace 'pi' with your username):
sudo chown -R "$USER":"$USER" .
rm -rf .venv  # if a partial venv exists
python3 -m venv .venv --system-site-packages
```

4) Verify the venv and imports

```bash
source .venv/bin/activate
python -c "import sys; print(sys.executable)"
python -c "import cv2, dlib, numpy as np; print('cv2', cv2.__version__, 'numpy', np.__version__)"
```

5) Do not pip-install OpenCV/dlib in this venv on the Pi

If you already installed them via pip, remove them so the apt versions are used:

```bash
pip uninstall -y opencv-python opencv-python-headless dlib numpy
python -c "import cv2, dlib; print('cv2 ok')"
```

### Fix: `ModuleNotFoundError: No module named 'cv2'`

This means the Python interpreter you’re using can’t see OpenCV.

1) Install OpenCV from apt (recommended on Raspberry Pi):

```bash
sudo apt update
sudo apt install -y python3-opencv
```

2) Make sure you’re running with the same Python that has OpenCV:

```bash
python3 -c "import sys, cv2; print(sys.executable); print(cv2.__version__)"
```

3) If you use a virtualenv, recreate it to include system packages:

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -c "import cv2; print(cv2.__version__)"
```

4) Avoid `pip install opencv-python` on the Pi—it’s large and may miss GStreamer/GUI pieces. If you must use pip, try `opencv-python-headless`, but note that GUI functions like `cv2.imshow` won’t work without additional GUI libs. Prefer the apt package whenever possible.

---

If you hit anything not covered here, tell me what failed and the exact error/output, and I’ll tailor the next steps.

