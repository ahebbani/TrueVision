SHELL := /bin/bash

PI_VENV ?= .venv-pi
SERVER_VENV ?= .venv-server
UVICORN_HOST ?= 0.0.0.0
UVICORN_PORT ?= 8008

PI_PYTHON := $(if $(wildcard $(PI_VENV)/bin/python),$(PI_VENV)/bin/python,python3)
SERVER_PYTHON := $(if $(wildcard $(SERVER_VENV)/bin/python),$(SERVER_VENV)/bin/python,python3)

.PHONY: run run-server setup-pi setup-server

run:
	$(PI_PYTHON) rpi.py

run-server:
	$(SERVER_PYTHON) -m uvicorn server:app --host $(UVICORN_HOST) --port $(UVICORN_PORT)

setup-pi:
	bash setup-pi.sh

setup-server:
	bash setup-server.sh