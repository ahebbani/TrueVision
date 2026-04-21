.PHONY: help run run-audio run-face run-esp32 run-esp32-force-both models fetch-models venv run-server run-server-dev setup-pi setup-server server-status backfill-trigger db db-report

# DB report defaults
DB_REPORT_LIMIT ?= 100

# Remote summarizer (FastAPI + Ollama). For local testing we default to localhost.
# Override/disable with: `make run SUMMARIZER_URL=`
SUMMARIZER_URL ?= http://127.0.0.1:8008
SUMMARIZER_TIMEOUT_SEC ?= 8
export SUMMARIZER_URL
export SUMMARIZER_TIMEOUT_SEC

# TrueVision server (transcription + summarization offload).
# Set on the Pi side to enable server offloading:
#   make run-esp32 TRUEVISION_SERVER_URL=http://192.168.1.100:8008
TRUEVISION_SERVER_URL ?=
TRUEVISION_SERVER_PORT ?= 8008
export TRUEVISION_SERVER_URL
export TRUEVISION_SERVER_PORT

VENV_PY := .venv/bin/python
ifeq ($(wildcard $(VENV_PY)), $(VENV_PY))
PY := $(VENV_PY)
else
PY ?= python3
endif

help:
	@echo "Targets:"
	@echo "  run                 - Run full system; mode switch is honored, BOTH needs server"
	@echo "  run-audio           - Force audio-only mode"
	@echo "  run-face            - Force face-only mode"
	@echo "  run-esp32           - Run with ESP32 UART audio; firmware mode packets are honored"
	@echo "  run-esp32-force-both - Request BOTH; actual BOTH requires reachable server"
	@echo "  run-server          - Start TrueVision server (transcription + summarization)"
	@echo "  run-server-dev      - Start server with auto-reload (development)"
	@echo "  server-status       - Check server health endpoint"
	@echo "  backfill-trigger    - Trigger backfill transcription on server"
	@echo "  db-report           - Generate HTML DB report under docs/ (limit via DB_REPORT_LIMIT=...)"
	@echo "  fetch-models        - Fetch facial recognition models"
	@echo "  models              - Alias for fetch-models"
	@echo "  setup-pi            - Run Raspberry Pi setup script"
	@echo "  setup-server        - Run server setup script (Linux + GPU)"

# Run commands to test all or parts of the codebase
run:
	$(PY) main.py --overlay-only

run-test:
	$(PY) main.py

run-audio:
	$(PY) main.py --serial-baud 921600 --force-mode audio

run-face:
	$(PY) main.py --serial-baud 921600 --force-mode face

run-overlay:
	$(PY) main.py --force-mode face --overlay-only

run-esp32:
	$(PY) main.py --serial-baud 921600

run-esp32-force-both:
	$(PY) main.py --serial-baud 921600 --force-mode both

fetch-models:
	$(PY) facial_recognition/models/fetch_models.py

# ── TrueVision Server ───────────────────────────────────────────────────

run-server:
	$(PY) -m server.app

run-server-dev:
	$(PY) -m uvicorn server.app:app --reload --host 0.0.0.0 --port $(TRUEVISION_SERVER_PORT)

server-status:
	@curl -sf http://127.0.0.1:$(TRUEVISION_SERVER_PORT)/health | python3 -m json.tool 2>/dev/null \
		|| echo "Server not reachable on port $(TRUEVISION_SERVER_PORT)"

backfill-trigger:
	@curl -sf -X POST http://127.0.0.1:$(TRUEVISION_SERVER_PORT)/api/backfill/trigger \
		| python3 -m json.tool 2>/dev/null \
		|| echo "Server not reachable on port $(TRUEVISION_SERVER_PORT)"

db:
	$(PY) data_access/visualize_db.py --html --limit $(DB_REPORT_LIMIT)

db-report: db

setup-pi:
	bash setup_pi.sh

setup-server:
	bash setup_server.sh

venv:
	source .venv/bin/activate