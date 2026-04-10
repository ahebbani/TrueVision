#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="truevision"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

# Repo root is parent of this script's directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

RUN_USER="${SUDO_USER:-${USER:-pi}}"

# Allow overriding the user and/or enabling xvfb wrapper
#   sudo ./scripts/install_truevision_systemd.sh --user pi --xvfb
USE_XVFB=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      RUN_USER="$2"; shift 2 ;;
    --xvfb)
      USE_XVFB=1; shift 1 ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

MAKE_BIN="$(command -v make || true)"
if [[ -z "$MAKE_BIN" ]]; then
  echo "make not found; install it: sudo apt install -y make" >&2
  exit 1
fi

PY_ARG=""
if [[ -x "${REPO_DIR}/.venv/bin/python3" ]]; then
  PY_ARG="PY=${REPO_DIR}/.venv/bin/python3"
fi

EXEC_START=()
if [[ $USE_XVFB -eq 1 ]]; then
  XVFB_BIN="$(command -v xvfb-run || true)"
  if [[ -z "$XVFB_BIN" ]]; then
    echo "xvfb-run not found; install it: sudo apt install -y xvfb" >&2
    exit 1
  fi
  EXEC_START=("$XVFB_BIN" -a "$MAKE_BIN" -C "$REPO_DIR" run-face)
else
  EXEC_START=("$MAKE_BIN" -C "$REPO_DIR" run-face)
fi

SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# Build ExecStart line (systemd requires it as a single string)
EXEC_START_LINE="${EXEC_START[*]}"
if [[ -n "$PY_ARG" ]]; then
  EXEC_START_LINE+=" ${PY_ARG}"
fi

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=TrueVision (make run-face)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${EXEC_START_LINE}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "$SERVICE_PATH"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "Installed and started: ${SERVICE_NAME}.service"
echo "Check status: systemctl status ${SERVICE_NAME} --no-pager"
echo "Follow logs:  journalctl -u ${SERVICE_NAME} -f"
