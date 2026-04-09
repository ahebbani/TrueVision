# Third-party dependencies by component

This document lists third-party software used by each component (facial_recognition, audio_analysis, oled_output, data_access) and describes how each is used in the project.

## facial_recognition

| Name | License | Description | Use |
|---|---|---|---|
| OpenCV (`opencv-python`) | BSD-3-Clause | Popular computer vision library providing image I/O, capture, filtering, color-space conversion, display windows, and GStreamer pipeline bindings. | Camera capture (OpenCV VideoCapture), frame manipulation (cvtColor, drawing rectangles/text), and optional GStreamer pipelines on Raspberry Pi.
| dlib | Boost Software License (BSL-1.0) | Toolkit containing state-of-the-art machine learning algorithms and utilities; used here for face detection, landmark prediction and face recognition model. | Face detection, facial landmark prediction (shape predictor) and creation of 128-d face embeddings used for recognition/matching.
| numpy | BSD-style | Core numerical array library for Python. | Represent images and embedding vectors, compute distances (np.linalg.norm), array conversions and buffering for DB storage.
| Picamera2 (optional) | Apache-2.0 (Raspberry Pi Foundation) | Raspberry Pi camera library used as an alternative capture backend when running on Pi hardware. | Optional camera backend if available; used to capture frames when OpenCV direct capture fails or Picamera2 is preferred.
| GStreamer (system-level) | LGPL (varies by plugin) | Multimedia framework leveraged via OpenCV GStreamer pipelines to access libcamera on Raspberry Pi. | Optional camera backend pipeline for libcamera-based capture (used when OpenCV is built with GStreamer support).

## audio_analysis

| Name | License | Description | Use |
|---|---|---|---|
| sounddevice | MIT License | Python bindings for the PortAudio library; provides cross-platform audio input/output streams. | `Recorder` uses sounddevice.InputStream to record live microphone audio to disk while people are present.
| soundfile (PySoundFile) | BSD-style | Python library wrapping libsndfile for reading/writing audio files (WAV/FLAC/etc.). | Used to write WAV audio files from the recorder (high-quality PCM_16 files saved to recordings directory).
| faster-whisper (optional) | MIT License | Optimized Whisper model wrapper for faster on-device transcription (can use CTranslate2 backends). | `Transcriber` uses faster-whisper (when installed) to transcribe recorded WAV files into text; transcription feature is optional and gracefully disabled if missing.
| CTranslate2 (optional backend) | Apache-2.0 | Inference engine used by some faster-whisper setups to accelerate model execution on CPU/GPU. | Optional acceleration/backend for faster-whisper; not required at runtime but recommended for performance on small devices.

## oled_output

| Name | License | Description | Use |
|---|---|---|---|
| luma.oled | MIT License | High-level drivers and helpers for small OLED displays (SSD1306, SH1106) built on top of luma.core. | Provides device drivers and high-level APIs to open the I2C device and draw pixels; used to initialize the OLED and render text in `oled_output.oled_display`.
| luma.core | MIT License | Core support library for luma.* drivers, including device interfaces and rendering utilities. | Used for I2C interface creation (i2c serial) and `canvas` drawing context used by the `_LumaDisplay` implementation.
| Pillow (PIL) | Pillow license (PIL/Historical) | Image processing library for Python; provides font loading and text rendering. | Used to load truetype fonts or default bitmap fonts and to draw text onto the OLED canvas.

## data_access / database

| Name | License | Description | Use |
|---|---|---|---|
| sqlite3 (Python stdlib) / SQLite | Public domain (SQLite) | Embedded SQL database engine and the Python stdlib wrapper. SQLite is a lightweight, serverless SQL DB stored in a single file. | Store `faces`, `face_embeddings`, and `meetings` tables. Holds embeddings (BLOBs), metadata (timestamps, seen counts), meeting audio paths, transcripts and summaries.

---

Notes and optional components

- Many components have optional dependencies to improve performance (e.g., `faster-whisper` and `CTranslate2` for transcription acceleration). The application is designed to run with transcription and OLED disabled if those optional packages or hardware drivers are missing.
- System-level multimedia/camera support (GStreamer, libcamera) and I2C access are provided by OS libraries and drivers; those are not Python packages and must be installed/configured on the host (e.g., `libcamera`, `i2c-tools`, kernel drivers).

If you want, I can:
- Add exact version pins and license file links for each dependency (I can read `requirements*.txt` and verify installed versions in the virtualenv)
- Add this table to the project README or link it from `SETUP_PI.md` to make installation guidance clearer