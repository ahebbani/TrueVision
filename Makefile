.PHONY: help run run-no-audio run-overlay-no-audio models venv run-summarizer summarizer-setup summarizer-run db db-report

# DB report defaults
DB_REPORT_LIMIT ?= 100

# Remote summarizer (FastAPI + Ollama). For local testing we default to localhost.
# Override/disable with: `make run SUMMARIZER_URL=`
SUMMARIZER_URL ?= http://127.0.0.1:8008
SUMMARIZER_TIMEOUT_SEC ?= 8
export SUMMARIZER_URL
export SUMMARIZER_TIMEOUT_SEC

VENV_PY := .venv/bin/python
ifeq ($(wildcard $(VENV_PY)), $(VENV_PY))
PY := $(VENV_PY)
else
PY ?= python3
endif

help:
	@echo "Targets:"
	@echo "  run                 - Run full system (video + audio)"
	@echo "  run-esp32           - Run with ESP32 UART audio + face recognition simultaneously (no mode gate)"
	@echo "  run-no-audio        - Run video only (disable audio/transcription)"
	@echo "  run-overlay-no-audio- Run overlay-only video with no audio (lighter)"
	@echo "  run-speak           - Run full system with spoken captions (TTS)"
	@echo "  run-summarizer      - Run off-device summarization service (FastAPI)"
	@echo "  summarizer-setup    - Install summarizer service deps (FastAPI/uvicorn/requests)"
	@echo "  summarizer-run      - Alias for run-summarizer"
	@echo "  db-report           - Generate HTML DB report under docs/ (limit via DB_REPORT_LIMIT=...)"
	@echo "  models              - Fetch facial recognition models"

run:
	$(PY) main.py

run-esp32:
	$(PY) main.py --audio-source esp32-serial --no-mode-gate --serial-baud 460800

run-speak:
	$(PY) main.py --speak-captions

run-no-audio:
	$(PY) main.py --no-audio

run-overlay-no-audio:
	$(PY) main.py --no-audio --overlay-only

models:
	$(PY) facial_recognition/models/fetch_models.py

run-summarizer:
	$(PY) -m summarization.server

db:
	$(PY) data_access/visualize_db.py --html --limit $(DB_REPORT_LIMIT)

db-report: db

summarizer-setup:
	$(PY) -m pip install -r requirements-summarization-service.txt
