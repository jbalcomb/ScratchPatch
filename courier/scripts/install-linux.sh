#!/usr/bin/env bash
# Install Courier's Phase A broker (ntfy) and gateway as systemd user services and publish both
# on your tailnet.
#
# Creates .venv with Courier's one dependency (cryptography); downloads ntfy into bin/, refusing
# it unless its SHA-256 matches the one pinned below; writes ~/.config/courier (gateway signing
# key, ntfy's server.yml with no anonymous access, a write-only gateway token and a read-only
# device token, all mode 600); installs and starts courier-ntfy and courier-gateway user services
# (restart on failure); and, if Tailscale is installed, runs `tailscale serve` for both ports.
# Rerunning it is safe: keys, tokens, senders and the audit log are kept.
#
#   scripts/install-linux.sh                 # gateway 8766, ntfy 2586
#   scripts/install-linux.sh 9766 9586
set -euo pipefail

gateway_port="${1:-8766}"
ntfy_port="${2:-2586}"

# Pinned: v2.35.0 (2026-10-30) fixes anonymous writes to protected topics via the Matrix gateway.
ntfy_version="2.35.0"
case "$(uname -m)" in
    x86_64)        arch=amd64; ntfy_sha256=da694a751d8c1681a39bd45682631837508dfd5721ad6226768617fac181a9bc ;;
    aarch64|arm64) arch=arm64; ntfy_sha256=c0dd07b61c42d28ce9b710750d9f6920041146f517f29da5ed4c00a1892062b3 ;;
    armv7l)        arch=armv7; ntfy_sha256=c514a8f4a9f9c894829f2d82fa6001545a4fd318202f28b946278ce83bb09d76 ;;
    *) echo "No ntfy build pinned for $(uname -m)." >&2; exit 1 ;;
esac

project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$project/src"
data="$project/data"
bin="$project/bin"
config="$HOME/.config/courier"
units="$HOME/.config/systemd/user"
ntfy="$bin/ntfy"

python="$(command -v python3 || true)"
[ -n "$python" ] || { echo "python3 not found." >&2; exit 1; }
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "$python is older than 3.11." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd not found." >&2; exit 1; }

# 1. venv
[ -x "$project/.venv/bin/python" ] || "$python" -m venv "$project/.venv" \
    || { echo "Creating .venv failed; on Debian/Ubuntu: sudo apt install python3-venv" >&2; exit 1; }
venv_python="$project/.venv/bin/python"
"$venv_python" -m pip install --quiet --disable-pip-version-check "cryptography>=42"

# 2. ntfy, verified against the pinned hash
if [ ! -x "$ntfy" ] || [ "$(cat "$bin/ntfy.version" 2>/dev/null)" != "$ntfy_version" ]; then
    name="ntfy_${ntfy_version}_linux_${arch}"
    url="https://github.com/binwiederhier/ntfy/releases/download/v${ntfy_version}/${name}.tar.gz"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "Downloading $url"
    "$python" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$tmp/ntfy.tar.gz"
    hash="$(sha256sum "$tmp/ntfy.tar.gz" | cut -d' ' -f1)"
    if [ "$hash" != "$ntfy_sha256" ]; then
        echo "ntfy download has SHA-256 $hash, expected $ntfy_sha256. Not installing it." >&2
        exit 1
    fi
    tar -xzf "$tmp/ntfy.tar.gz" -C "$tmp" "$name/ntfy"
    systemctl --user stop courier-ntfy.service 2>/dev/null || true
    mkdir -p "$bin"
    install -m 755 "$tmp/$name/ntfy" "$ntfy"
    echo "$ntfy_version" > "$bin/ntfy.version"
fi

# 3. Keys, tokens and ntfy's config, readable only by you
mkdir -p "$config" "$data" "$units"
chmod 700 "$config"
gateway_key="$(cd "$src" && "$venv_python" -m courier gateway-key)"
(cd "$src" && "$venv_python" -m courier ntfy-setup --ntfy-exe "$ntfy" --listen "127.0.0.1:$ntfy_port" >/dev/null)

# 4. systemd user services
cat > "$units/courier-ntfy.service" <<EOF
[Unit]
Description=Courier broker (ntfy, private: tokens only)
After=network-online.target

[Service]
ExecStart=$ntfy serve --config $config/ntfy/server.yml
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
cat > "$units/courier-gateway.service" <<EOF
[Unit]
Description=Courier gateway: screens, signs and publishes messages to ntfy
After=courier-ntfy.service
Wants=courier-ntfy.service

[Service]
WorkingDirectory=$src
ExecStart=$venv_python -m courier serve --port $gateway_port --ntfy http://127.0.0.1:$ntfy_port --ntfy-token-file $config/ntfy-gateway.token --data $data --log-file $data/gateway.log
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable courier-ntfy.service courier-gateway.service >/dev/null
systemctl --user restart courier-ntfy.service courier-gateway.service

# Without lingering, user services stop when you log out and don't start at boot.
loginctl enable-linger "$USER" 2>/dev/null \
    || echo "Note: run 'sudo loginctl enable-linger $USER' so Courier starts at boot and survives logout."

wait_healthy() {  # url, json field
    for _ in $(seq 15); do
        sleep 1
        if "$python" -c "import json,sys,urllib.request; sys.exit(not json.load(urllib.request.urlopen(sys.argv[1], timeout=2)).get(sys.argv[2]))" "$1" "$2" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}
wait_healthy "http://127.0.0.1:$ntfy_port/v1/health" healthy \
    || { echo "ntfy didn't come up on port $ntfy_port. See: journalctl --user -u courier-ntfy" >&2; exit 1; }
wait_healthy "http://127.0.0.1:$gateway_port/health" ok \
    || { echo "The gateway didn't come up on port $gateway_port. See: journalctl --user -u courier-gateway, $data/gateway.log" >&2; exit 1; }
echo "ntfy is up on http://127.0.0.1:$ntfy_port and the gateway on http://127.0.0.1:$gateway_port."

# 5. Tailscale
base="https://<this machine>.<tailnet>.ts.net"
if ! command -v tailscale >/dev/null; then
    echo "Tailscale isn't installed. Once it is:"
    echo "  tailscale serve --bg --https=$gateway_port http://127.0.0.1:$gateway_port"
    echo "  tailscale serve --bg --https=$ntfy_port http://127.0.0.1:$ntfy_port"
elif tailscale serve --bg --https="$gateway_port" "http://127.0.0.1:$gateway_port" \
    && tailscale serve --bg --https="$ntfy_port" "http://127.0.0.1:$ntfy_port"; then
    base="https://$(tailscale status --json | "$python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
else
    echo "tailscale serve failed (above). If it's a permission error: sudo tailscale set --operator=$USER, then rerun."
fi

cat <<EOF

Gateway URL:   $base:$gateway_port
ntfy URL:      $base:$ntfy_port   (devices subscribe here)
Gateway key:   $gateway_key   (devices verify envelopes with this)
Device token:  $config/ntfy-device.token   (read-only; copy it to each device agent)

Add a sender:  cd $src && ../.venv/bin/python -m courier add-sender NAME --kind script --channels clip --gateway $base:$gateway_port
Logs:          journalctl --user -u courier-ntfy -u courier-gateway, $data/gateway.log, $data/audit.jsonl
Remove:        scripts/uninstall-linux.sh
EOF
