#!/usr/bin/env bash
# Install the Courier device agent as a LaunchAgent, so it runs in your login session (the
# clipboard and notifications need it) and launchd restarts it if it exits.
#
# Creates .venv, writes ~/.config/courier/device.toml if there is none, and installs and starts
# ~/Library/LaunchAgents/net.courier.device.plist. Uses pbcopy/pbpaste and osascript, which come
# with macOS. Copy ntfy-device.token from the gateway machine's ~/.config/courier first.
# Rerunning it is safe; an existing device.toml is kept.
#
#   scripts/install-device-macos.sh https://box.tailnet.ts.net:2586 <gateway key>
set -euo pipefail

ntfy="${1:-}"
gateway_key="${2:-}"
label="net.courier.device"
project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$project/src"
data="$project/data"
config="$HOME/.config/courier"
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

[ "$(uname -s)" = Darwin ] || { echo "This script is for macOS." >&2; exit 1; }
python=""
for candidate in python3.13 python3.12 python3.11 python3; do
    p="$(command -v "$candidate" || true)"
    if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python="$p"; break
    fi
done
[ -n "$python" ] || { echo "Python 3.11 or newer not found (brew install python@3.12)." >&2; exit 1; }

[ -x "$project/.venv/bin/python" ] || "$python" -m venv "$project/.venv"
venv_python="$project/.venv/bin/python"
"$venv_python" -m pip install --quiet --disable-pip-version-check "cryptography>=42"

mkdir -p "$config" "$data" "$(dirname "$plist")"
chmod 700 "$config"
[ -f "$config/ntfy-device.token" ] \
    || { echo "No device token at $config/ntfy-device.token. Copy it from the gateway machine." >&2; exit 1; }

if [ ! -f "$config/device.toml" ]; then
    [ -n "$ntfy" ] || { echo "Usage: $0 https://<broker>.<tailnet>.ts.net:2586 [gateway key]" >&2; exit 1; }
    args=(device-init --ntfy "$ntfy")
    [ -n "$gateway_key" ] && args+=(--gateway-key "$gateway_key")
    (cd "$src" && "$venv_python" -m crossmess "${args[@]}" >/dev/null)
    echo "Wrote $config/device.toml."
else
    echo "Kept $config/device.toml."
fi

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml "$venv_python")</string>
        <string>-m</string>
        <string>crossmess</string>
        <string>device</string>
        <string>--data</string>
        <string>$(xml "$data")</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- pbcopy and pbpaste decode by locale; launchd sets none, which mangles non-ASCII -->
        <key>LANG</key><string>en_US.UTF-8</string>
    </dict>
    <key>WorkingDirectory</key><string>$(xml "$src")</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>$(xml "$data/device.out.log")</string>
    <key>StandardErrorPath</key><string>$(xml "$data/device.err.log")</string>
</dict>
</plist>
EOF
chmod 644 "$plist"
plutil -lint "$plist" >/dev/null

launchctl bootout "$domain/$label" 2>/dev/null || true
launchctl bootstrap "$domain" "$plist"
launchctl enable "$domain/$label"

cat <<EOF
Courier Device is installed (LaunchAgent $label, started at login).

Policy:  $config/device.toml   (launchctl kickstart -k $domain/$label after editing it)
Inbox:   cd $src && ../.venv/bin/python -m crossmess inbox
Logs:    $data/device.err.log, $data/device.jsonl
Remove:  scripts/uninstall-device-macos.sh

The first notification may ask to allow notifications from Script Editor (osascript):
allow it in System Settings > Notifications.
EOF
