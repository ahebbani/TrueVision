# TrueVision
Senior design smart glasses project.

## Setup quickstart

- Raspberry Pi 4B users: follow `facial_recognition/SETUP_PI.md`. In short, install OpenCV and dlib from apt, not pip. Then install Python deps with:

```bash
pip install -r requirements.txt
```

- Desktop dev (Linux/macOS/Windows):

```bash
pip install -r requirements.txt
# Add desktop extras (OpenCV, dlib, optional faster-whisper):
pip install -r requirements-desktop.txt
```

Optional: to add speech-to-text only, use:

```bash
pip install -r requirements-faster-whisper.txt
```

## Project layout (after refactor)

```
facial_recognition/        # Face detection & recognition (models + helper scripts)
audio_analysis/            # Transcription & summarization (Whisper wrapper)
oled_output/               # Optional SSD1306 OLED display integration & test harness
data_access/               # faces.db + centralized schema & helpers
data/                      # recordings/ (audio WAV files per meeting)
main.py                    # Top-level application entry point
```

Run commands from the repository root so Python can discover sibling packages.

## OLED display

An SSD1306 128x64 OLED (like Adafruit’s 0.96" STEMMA QT) can mirror on-screen labels (name, seen count, last seen, REC). Enable with:

```bash
OLED=1 python3 main.py
```

See `facial_recognition/SETUP_PI.md` section "Optional: SSD1306 OLED" for wiring and environment variables.

Test the OLED module independently (after setting env vars) with:

```bash
OLED=1 python -m oled_output.test_oled
```

## Transcription (audio analysis)

Whisper-based recording & transcription lives in `audio_analysis/transcription.py`.
It is imported dynamically by `facial_recognition/main.py`; if dependencies (sounddevice, faster-whisper) are missing, the app continues with transcription disabled.

To install only the extra speech-to-text dependencies:

```bash
pip install -r requirements-faster-whisper.txt
```

## Running main application

From repo root:

```bash
python main.py
```

Press `t` inside the app window to toggle transcription at runtime; press `q` to quit.

## Getting Started: Raspberry Pi vs Desktop

Choose the path that matches your environment. Run commands from the repo root so Python can discover sibling packages.

- Raspberry Pi (recommended for hardware integration):
	- Install OpenCV and dlib via apt (per `facial_recognition/SETUP_PI.md`).
	- Install Python deps:
		```bash
		pip install -r requirements.txt
		```
	- Optional: enable OLED and test it:
		```bash
		OLED=1 python -m oled_output.test_oled
		```
	- Run the app:
		```bash
		python main.py
		```
	- Optional: add speech-to-text (sizeable install):
		```bash
		pip install -r requirements-faster-whisper.txt
		```

- Desktop (Linux/macOS/Windows):
	- Install core deps:
		```bash
		pip install -r requirements.txt
		```
	- Add desktop extras (OpenCV, dlib, optional faster-whisper):
		```bash
		pip install -r requirements-desktop.txt
		```
	- Run the app:
		```bash
		python main.py
		```
	- Notes: OLED and Pi-specific camera backends may be unavailable; the app falls back gracefully. If OpenCV build lacks GStreamer, set `CAMERA_BACKEND=opencv`.

## Database & audio linkage

Audio recordings are stored per meeting in the `meetings` table (columns: `person_id`, `audio_path`, `transcript`, `summary`). Each meeting row links back to a person in `faces`. We avoid duplicating audio paths in `faces`; multiple meetings can exist per person. Shared schema + pruning logic lives in `data_access/db.py`. The SQLite file `faces.db` now resides directly in `data_access/`. Audio files saved under `data/recordings/`.
