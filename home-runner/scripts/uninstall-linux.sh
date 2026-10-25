#!/usr/bin/env bash
# Stop and remove the Home Runner service and its `tailscale serve` entry. Run history and the key are kept.
set -euo pipefail
port="${1:-8765}"
unit="$HOME/.config/systemd/user/home-runner.service"

if [ -f "$unit" ]; then
    systemctl --user disable --now home-runner.service || true
    rm -f "$unit"
    systemctl --user daemon-reload
    echo "Removed the home-runner service."
else
    echo "No home-runner service installed."
fi
if command -v tailscale >/dev/null && tailscale serve --https="$port" off 2>/dev/null; then
    echo "Removed the tailscale serve entry for port $port."
fi
echo "Kept: data/ (run history, logs) and ~/.config/home-runner/key."
