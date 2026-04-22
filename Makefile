.PHONY: help run run-test run-audio run-face run-overlay run-esp32 run-esp32-force-both models fetch-models venv run-server run-server-dev setup-pi setup-server server-status backfill-trigger db db-report

# DB report defaults
DB_REPORT_LIMIT ?= 100

# TrueVision server (transcription + summarization offload).
# On the Pi, `make run` will use this URL if set; otherwise it will try mDNS
# discovery for `TrueVision Server._truevision._tcp.local.` on the local LAN.
# Example explicit override:
#   make run TRUEVISION_SERVER_URL=http://dgx-spark.local:8008
TRUEVISION_SERVER_URL ?= http://10.186.71.82:8008
TRUEVISION_SERVER_PORT ?= 8008
export TRUEVISION_SERVER_URL
export TRUEVISION_SERVER_PORT

# Remote summarizer endpoint. By default this follows the main TrueVision
# server URL so LLM summarization stays on the server side.
# Override/disable with: `make run SUMMARIZER_URL=`
SUMMARIZER_URL ?= $(TRUEVISION_SERVER_URL)
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
	@echo "  run                 - Run with automatic device detection and server offload if configured/discovered"
	@echo "  run-test            - Alias for plain main.py runtime"
	@echo "  run-audio           - Force audio-only mode"
	@echo "  run-face            - Force face-only mode"
	@echo "  run-overlay         - Force face-only overlay mode"
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
	$(PY) main.py

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