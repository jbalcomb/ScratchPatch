#!/usr/bin/env bash
# Install the Discord Capture sync as a cron job for the current user.
#
# Saves the bot token (prompting for it if needed) to ~/.config/discord-capture/token
# with mode 600, checks it against Discord, then adds a crontab line that runs
# `discord_capture sync` every MINUTES minutes. Rerunning it replaces the line.
#
#   scripts/install-linux.sh              # hourly
#   scripts/install-linux.sh 30           # every 30 minutes
#   scripts/install-linux.sh 60 --new-token
set -euo pipefail

minutes="${1:-60}"
new_token="${2:-}"
marker="# discord-capture sync"

project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$project/src"
data="$project/data"
db="$data/discord.sqlite3"
log="$data/sync.log"
token_dir="$HOME/.config/discord-capture"
token_file="$token_dir/token"

# Cron can only express intervals that divide an hour or a day evenly.
# Minute 7 rather than :00, so we're not one of the crowd hitting Discord on the hour.
case "$minutes" in
    5|10|15|20|30) schedule="*/$minutes * * * *" ;;
    60)            schedule="7 * * * *" ;;
    120|180|240|360|480|720) schedule="7 */$((minutes / 60)) * * *" ;;
    1440)          schedule="7 4 * * *" ;;
    *) echo "MINUTES must be one of 5 10 15 20 30 60 120 180 240 360 480 720 1440" >&2; exit 1 ;;
esac

python="$(command -v python3 || true)"
[ -n "$python" ] || { echo "python3 not found." >&2; exit 1; }
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || { echo "$python is older than 3.10." >&2; exit 1; }
command -v crontab >/dev/null || { echo "crontab not found (install cron/cronie)." >&2; exit 1; }

mkdir -p "$data" "$token_dir"
chmod 700 "$token_dir"

if [ "$new_token" = "--new-token" ] || [ ! -s "$token_file" ]; then
    read -rsp "Paste the bot token (Developer Portal > your app > Bot > Reset Token): " token
    echo
    [ -n "$token" ] || { echo "No token entered." >&2; exit 1; }
    (umask 077 && printf '%s' "$token" > "$token_file")
    echo "Token saved to $token_file"
fi

echo "Checking the token against Discord..."
(cd "$src" && "$python" -m discord_capture channels --db "$db") \
    || { echo "The check failed; not installing the cron job. Fix the token with --new-token." >&2; exit 1; }

q() { local sq="'" esc="'\\''"; printf "'%s'" "${1//$sq/$esc}"; }  # single-quote for the crontab line
line="$schedule cd $(q "$src") && $(q "$python") -m discord_capture sync --db $(q "$db") --log-file $(q "$log") $marker"
{ crontab -l 2>/dev/null | grep -vF "$marker" || true; echo "$line"; } | crontab -

cat <<EOF

Installed cron job: $schedule
  Log:      $log
  Database: $db
  Remove:   scripts/uninstall-linux.sh

For the historic catch-up, run this once (resumable; the scheduled sync waits while it runs):
  cd '$src' && $python -m discord_capture backfill --db '$db' -v
EOF
