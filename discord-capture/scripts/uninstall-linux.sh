#!/usr/bin/env bash
# Remove the Discord Capture cron job. The database, log and token are left alone.
set -euo pipefail
marker="# discord-capture sync"

if crontab -l 2>/dev/null | grep -qF "$marker"; then
    { crontab -l | grep -vF "$marker" || true; } | crontab -
    echo "Removed the discord-capture cron job."
else
    echo "No discord-capture cron job installed."
fi
echo "Kept: data/ (database, log) and ~/.config/discord-capture/token. Delete them by hand if you want them gone."
