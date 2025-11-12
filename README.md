# TrueVision
Senior design smart glasses project.

## Setup quickstart

- Raspberry Pi 4B users: follow `facial_recognition/SETUP_PI.md`. In short, install OpenCV and dlib from apt, not pip. Then install Python deps with:

```bash
cd facial_recognition
pip install -r requirements.txt
```

- Desktop dev (Linux/macOS/Windows):

```bash
cd facial_recognition
pip install -r requirements.txt
# Add desktop extras (OpenCV, dlib, optional faster-whisper):
pip install -r requirements-desktop.txt
```

Optional: to add speech-to-text only, use:

```bash
pip install -r requirements-faster-whisper.txt
```

## OLED display

An SSD1306 128x64 OLED (like Adafruit’s 0.96" STEMMA QT) can mirror on-screen labels (name, seen count, last seen, REC). Enable with:

```bash
cd facial_recognition
OLED=1 python3 main.py
```

See `facial_recognition/SETUP_PI.md` section "Optional: SSD1306 OLED" for wiring and environment variables.
