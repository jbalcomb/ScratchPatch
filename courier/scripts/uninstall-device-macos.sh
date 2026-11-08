#!/usr/bin/env bash
# Stop and remove the Courier device LaunchAgent. device.toml, the token and data/ (inbox, logs) are kept.
set -euo pipefail
label="net.courier.device"
plist="$HOME/Library/LaunchAgents/$label.plist"

if [ -f "$plist" ]; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$plist"
    echo "Removed the $label LaunchAgent."
else
    echo "No $label LaunchAgent installed."
fi
echo "Kept: ~/.config/courier (device.toml, token) and data/ (inbox, logs)."
