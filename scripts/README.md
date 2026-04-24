scripts
=======

Purpose
- Deployment and system integration helpers (systemd service files and install scripts)
  to run the Pi application and server as services.

Files
- `install_truevision_systemd.sh` and `install_truevision_server_systemd.sh` — install
  service files and enable them via `systemctl`.
- `truevision.service`, `truevision-server.service` — systemd unit templates used by the
  installer. Replace `PLACEHOLDER_*` fields before installing or use the installers which
  prompt/patch these values.

Usage
- Edit the templates or use the installer scripts. The server service starts
  `python -m server.app` inside a virtualenv; the Pi service runs `main.py`.
