#!/usr/bin/env bash
set -e

export DISPLAY=:0
export XDG_SESSION_TYPE=x11
export XAUTHORITY="$HOME/.Xauthority"

cd "$HOME/TrueVision"

pkill -f firefox || true
pkill -f chromium || true
pkill -f "python3 rpi.py" || true
pkill -f "python rpi.py" || true

sleep 4

if [ -d "$HOME/TrueVision/venv" ]; then
    source "$HOME/TrueVision/venv/bin/activate"
elif [ -d "$HOME/TrueVision/.venv-pi" ]; then
    source "$HOME/TrueVision/.venv-pi/bin/activate"
else
    echo "No Pi virtual environment found in ~/TrueVision/venv or ~/TrueVision/.venv-pi"
    exit 1
fi

python3 rpi.py
