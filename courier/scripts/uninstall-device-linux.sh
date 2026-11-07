#!/usr/bin/env bash
# Stop and remove the courier-device service. device.toml, the token and data/ (inbox, logs) are kept.
set -euo pipefail
unit="$HOME/.config/systemd/user/courier-device.service"

if [ -f "$unit" ]; then
    systemctl --user disable --now courier-device.service || true
    rm -f "$unit"
    systemctl --user daemon-reload
    echo "Removed the courier-device service."
else
    echo "No courier-device service installed."
fi
echo "Kept: ~/.config/courier (device.toml, token) and data/ (inbox, logs)."
