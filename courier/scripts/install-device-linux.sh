#!/usr/bin/env bash
# Install the Courier device agent as a systemd user service tied to your graphical session
# (the clipboard and notifications need it).
#
# Creates .venv, writes ~/.config/courier/device.toml if there is none, and installs and starts
# courier-device.service. Needs wl-copy (Wayland) or xclip (X11) for the clipboard and notify-send
# for notifications. Copy ntfy-device.token from the gateway machine's ~/.config/courier first.
# Rerunning it is safe; an existing device.toml is kept.
#
#   scripts/install-device-linux.sh https://box.tailnet.ts.net:2586 <gateway key>
set -euo pipefail

ntfy="${1:-}"
gateway_key="${2:-}"
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$project/src"
data="$project/data"
config="$HOME/.config/courier"
unit="$HOME/.config/systemd/user/courier-device.service"

python="$(command -v python3 || true)"
[ -n "$python" ] || { echo "python3 not found." >&2; exit 1; }
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "$python is older than 3.11." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd not found." >&2; exit 1; }
command -v wl-copy >/dev/null || command -v xclip >/dev/null \
    || echo "Note: install wl-clipboard (Wayland) or xclip (X11), or clipboard actions will fail."
command -v notify-send >/dev/null || echo "Note: install libnotify (notify-send), or notifications will fail."

[ -x "$project/.venv/bin/python" ] || "$python" -m venv "$project/.venv" \
    || { echo "Creating .venv failed; on Debian/Ubuntu: sudo apt install python3-venv" >&2; exit 1; }
venv_python="$project/.venv/bin/python"
"$venv_python" -m pip install --quiet --disable-pip-version-check "cryptography>=42"

mkdir -p "$config" "$data" "$(dirname "$unit")"
chmod 700 "$config"
[ -f "$config/ntfy-device.token" ] \
    || { echo "No device token at $config/ntfy-device.token. Copy it from the gateway machine." >&2; exit 1; }

if [ ! -f "$config/device.toml" ]; then
    [ -n "$ntfy" ] || { echo "Usage: $0 https://<broker>.<tailnet>.ts.net:2586 [gateway key]" >&2; exit 1; }
    args=(device-init --ntfy "$ntfy")
    [ -n "$gateway_key" ] && args+=(--gateway-key "$gateway_key")
    (cd "$src" && "$venv_python" -m courier "${args[@]}" >/dev/null)
    echo "Wrote $config/device.toml."
else
    echo "Kept $config/device.toml."
fi

cat > "$unit" <<EOF
[Unit]
Description=Courier device agent: acts on messages per $config/device.toml
PartOf=graphical-session.target
After=graphical-session.target

[Service]
WorkingDirectory=$src
ExecStart=$venv_python -m courier device --data $data
Restart=always
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user enable courier-device.service >/dev/null
systemctl --user restart courier-device.service

cat <<EOF
Courier Device is installed (systemd user service courier-device, started with your desktop session).

Policy:  $config/device.toml   (systemctl --user restart courier-device after editing it)
Inbox:   cd $src && ../.venv/bin/python -m courier inbox
Logs:    journalctl --user -u courier-device, $data/device.jsonl
Remove:  scripts/uninstall-device-linux.sh
EOF
