.PHONY: help run run-no-audio run-overlay-no-audio models venv

PY ?= python3

help:
	@echo "Targets:"
	@echo "  run                 - Run full system (video + audio)"
	@echo "  run-no-audio        - Run video only (disable audio/transcription)"
	@echo "  run-overlay-no-audio- Run overlay-only video with no audio (lighter)"
	@echo "  run-speak           - Run full system with spoken captions (TTS)"
	@echo "  models              - Fetch facial recognition models"

run:
	$(PY) main.py

run-speak:
	$(PY) main.py --speak-captions

run-no-audio:
	$(PY) main.py --no-audio

run-overlay-no-audio:
	$(PY) main.py --no-audio --overlay-only

models:
	$(PY) facial_recognition/models/fetch_models.py
