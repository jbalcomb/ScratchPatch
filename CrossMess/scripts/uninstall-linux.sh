#!/usr/bin/env bash
# Stop and remove the Courier services and their `tailscale serve` entries. Keys, tokens, senders,
# bin/ntfy and data/ (logs, audit log, message cache) are kept.
set -euo pipefail
gateway_port="${1:-8766}"
ntfy_port="${2:-2586}"
units="$HOME/.config/systemd/user"

for service in courier-gateway courier-ntfy; do
    if [ -f "$units/$service.service" ]; then
        systemctl --user disable --now "$service.service" || true
        rm -f "$units/$service.service"
        echo "Removed the $service service."
    else
        echo "No $service service installed."
    fi
done
systemctl --user daemon-reload
if command -v tailscale >/dev/null; then
    for port in "$gateway_port" "$ntfy_port"; do
        tailscale serve --https="$port" off 2>/dev/null && echo "Removed the tailscale serve entry for port $port."
    done
fi
echo "Kept: ~/.config/courier (keys, tokens, senders), bin/ and data/."
