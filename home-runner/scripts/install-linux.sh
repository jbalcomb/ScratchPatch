#!/usr/bin/env bash
# Install Home Runner as a systemd user service and publish it on your tailnet.
#
# Creates the access key (~/.config/home-runner/key, mode 600), installs and starts
# ~/.config/systemd/user/home-runner.service (restarts on failure), and, if Tailscale is
# installed, runs `tailscale serve` so the phone reaches it at
# https://<this machine>.<tailnet>.ts.net:PORT. Rerunning it is safe; the key is kept.
#
#   scripts/install-linux.sh            # port 8765
#   scripts/install-linux.sh 9000
set -euo pipefail

port="${1:-8765}"
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$project/src"
log="$project/data/runner.log"
unit="$HOME/.config/systemd/user/home-runner.service"

python="$(command -v python3 || true)"
[ -n "$python" ] || { echo "python3 not found." >&2; exit 1; }
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "$python is older than 3.11." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd not found; run '$python -m home_runner serve' from $src your own way." >&2; exit 1; }

mkdir -p "$project/data" "$(dirname "$unit")"
key="$(cd "$src" && "$python" -m home_runner key)"

cat > "$unit" <<EOF
[Unit]
Description=Home Runner: runs the jobs in $project/jobs.toml on request
After=network-online.target

[Service]
WorkingDirectory=$src
ExecStart=$python -m home_runner serve --port $port --log-file $log
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable home-runner.service >/dev/null
systemctl --user restart home-runner.service

# Without lingering, user services stop when you log out and don't start at boot.
loginctl enable-linger "$USER" 2>/dev/null \
    || echo "Note: run 'sudo loginctl enable-linger $USER' so the runner starts at boot and survives logout."

up=""
for _ in $(seq 15); do
    sleep 1
    if "$python" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$port/health', timeout=2)" 2>/dev/null; then
        up=1; break
    fi
done
[ -n "$up" ] || { echo "The runner didn't come up on port $port. See: journalctl --user -u home-runner, $log" >&2; exit 1; }
echo "Home Runner is up on http://127.0.0.1:$port (systemd user service home-runner)."

url="https://<this machine>.<tailnet>.ts.net:$port"
if ! command -v tailscale >/dev/null; then
    echo "Tailscale isn't installed. Once it is: tailscale serve --bg --https=$port http://127.0.0.1:$port"
elif tailscale serve --bg --https="$port" "http://127.0.0.1:$port"; then
    name="$(tailscale status --json | "$python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    url="https://$name:$port"
else
    echo "tailscale serve failed (above). If it's a permission error: sudo tailscale set --operator=$USER, then rerun."
fi

cat <<EOF

In TaskDeck, open Home Jobs > settings (tune icon) and enter:
  Runner URL:  $url
  Runner key:  $key

Key file: ~/.config/home-runner/key (new key: python3 -m home_runner key --new, then systemctl --user restart home-runner)
Log:      $log
Remove:   scripts/uninstall-linux.sh
EOF
