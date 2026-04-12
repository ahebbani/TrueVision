#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Install the TrueVision Server as a systemd service.
# Usage:  sudo ./scripts/install_truevision_server_systemd.sh [--user USER]
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="truevision-server"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_USER="${SUDO_USER:-${USER:-$(whoami)}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

PY_BIN="${REPO_DIR}/.venv/bin/python"
if [[ ! -x "$PY_BIN" ]]; then
  echo "Python venv not found at $PY_BIN" >&2
  echo "Run 'bash setup_server.sh' first." >&2
  exit 1
fi

SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=TrueVision Server (transcription + summarization)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PY_BIN} -m server.app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
echo "Installed ${SERVICE_PATH}"
echo ""
echo "  Start:   sudo systemctl start ${SERVICE_NAME}"
echo "  Status:  sudo systemctl status ${SERVICE_NAME}"
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
