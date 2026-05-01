SHELL := /bin/bash

PI_VENV ?= .venv-pi
SERVER_VENV ?= .venv-server
UVICORN_HOST ?= 0.0.0.0
UVICORN_PORT ?= 8008

PI_VENV_ABS := $(abspath $(PI_VENV))
SERVER_VENV_ABS := $(abspath $(SERVER_VENV))


.PHONY: run run-server setup-pi setup-server

run:
	@if [ ! -x "$(PI_VENV_ABS)/bin/python" ]; then \
		echo "[make] Pi virtual environment missing at $(PI_VENV_ABS). Run 'make setup-pi' first."; \
		exit 1; \
	fi
	. "$(PI_VENV_ABS)/bin/activate" && python rpi.py

run-server:
	@if [ ! -x "$(SERVER_VENV_ABS)/bin/python" ]; then \
		echo "[make] Server virtual environment missing at $(SERVER_VENV_ABS). Run 'make setup-server' first."; \
		exit 1; \
	fi
	. "$(SERVER_VENV_ABS)/bin/activate" && \
		WHISPER_DEVICE=$${WHISPER_DEVICE:-cpu} \
		WHISPER_COMPUTE_TYPE=$${WHISPER_COMPUTE_TYPE:-int8} \
		python -m uvicorn server:app --host $(UVICORN_HOST) --port $(UVICORN_PORT)

setup-pi:
	PI_VENV_DIR="$(PI_VENV_ABS)" bash setup-pi.sh

setup-server:
	SERVER_VENV_DIR="$(SERVER_VENV_ABS)" bash setup-server.sh