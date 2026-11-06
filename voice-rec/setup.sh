#!/usr/bin/env bash
# Set up (or update) the voice-rec server on the AI Workstation. Safe to run again: it keeps your
# token, database password and vault choice, rebuilds, and restarts.
#
# From the phone (Termius), paste:
#
#     cd ~/develai/ScratchPatch && git checkout main && git pull && bash voice-rec/setup.sh
#
# Options (environment variables): VAULT=/path/to/vault  NO_SERVE=1 (skip Tailscale Serve)
#                                  USE_GPU=1 (Whisper on the GPU; off by default: llama.cpp's model fills it)
#                                  WHISPER_MODEL=small|medium|large-v3-turbo
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$HOME/.config/secrets.env"
ENVFILE="$HERE/.env"
# Ports: the first free one from these, saved in .env so later runs keep them (see pick_port / pick_https_port).
# Taken on the workstation: 8090 status-api, 8092 budget, 8093 photo-ocr, 8094; HTTPS 443 budget, 8444 photo-ocr.
FIRST_PORT=8095     # the server, on localhost
FIRST_DB_PORT=5433  # its Postgres, on localhost
FIRST_HTTPS=8445    # Tailscale Serve
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# Read KEY from a KEY=value file (empty if missing).
get() { [ -f "$2" ] && sed -n "s/^$1=//p" "$2" | tail -n1 || true; }
# Set KEY=value in a file (replacing an existing line).
put() {
  touch "$3"
  if grep -q "^$1=" "$3"; then
    local tmp; tmp="$(mktemp)"; awk -v k="$1" -v v="$2" -F= '$1==k{print k"="v; next}{print}' "$3" > "$tmp"
    cat "$tmp" > "$3"; rm -f "$tmp"
  else
    printf '%s=%s\n' "$1" "$2" >> "$3"
  fi
}
# Free = this script can open it on localhost (fails for anything listening there, Docker's published ports too).
port_free() {
  python3 -c 'import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)' "$1"
}
# The saved port if it is free or voice-rec already has it, else the first free one from there (or the default).
pick_port() {
  local name="$1" p saved; saved="$(get "$1" "$ENVFILE")"
  if [ -n "$saved" ] && { [ -n "$RUNNING" ] || port_free "$saved"; }; then echo "$saved"; return; fi
  p="${saved:-$2}"
  while ! port_free "$p"; do p=$((p + 1)); done
  echo "$p"
}
# Tailscale Serve HTTPS ports in use, except one that already points at our server.
serve_taken() {
  sudo tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
port = sys.argv[1]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit()
ours = {k.rsplit(":", 1)[-1] for k, w in (d.get("Web") or {}).items()
        if any(str(h.get("Proxy", "")).endswith(":" + port) for h in (w.get("Handlers") or {}).values())}
print(" ".join(p for p in (d.get("TCP") or {}) if p not in ours))' "$1"
}
random() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

say "Checking tools"
command -v docker >/dev/null || die "Docker isn't installed (the budget app uses it too: https://docs.docker.com/engine/install/ubuntu/)"
docker compose version >/dev/null 2>&1 || die "'docker compose' isn't available; install the Docker Compose plugin"
docker info >/dev/null 2>&1 || die "Can't talk to Docker. Add yourself to the docker group (sudo usermod -aG docker \$USER, then log in again)"
command -v python3 >/dev/null || die "python3 is needed for this script"
echo "docker: $(docker --version)"

say "Secrets ($SECRETS)"
mkdir -p "$HOME/.config" && touch "$SECRETS" && chmod 600 "$SECRETS"
TOKEN="$(get VOICE_REC_TOKEN "$SECRETS")"
if [ -z "$TOKEN" ]; then
  TOKEN="$(random)"; put VOICE_REC_TOKEN "$TOKEN" "$SECRETS"; echo "Created VOICE_REC_TOKEN"
else
  echo "VOICE_REC_TOKEN already set"
fi
[ -n "$(get LLM_URL "$SECRETS")" ] && echo "LLM_URL is set: notes will be categorized by llama.cpp" \
  || echo "LLM_URL isn't set: notes are archived to Inbox without categories (see README)"
[ -n "$(get GRAPH_KIND "$SECRETS")" ] && echo "Knowledge graph: $(get GRAPH_KIND "$SECRETS")" \
  || echo "Knowledge graph: off for now (set GRAPH_KIND / GRAPH_URL in secrets.env, see README)"

say "Settings ($ENVFILE)"
touch "$ENVFILE" && chmod 600 "$ENVFILE"
[ -n "$(get VOICE_REC_DB_PASSWORD "$ENVFILE")" ] || put VOICE_REC_DB_PASSWORD "$(random)" "$ENVFILE"
put UID "$(id -u)" "$ENVFILE"
put GID "$(id -g)" "$ENVFILE"

VAULT_DIR="${VAULT:-$(get OBSIDIAN_VAULT_DIR "$ENVFILE")}"
if [ -z "$VAULT_DIR" ]; then
  DEFAULT="$HOME/Obsidian/Notes"
  if [ -t 0 ]; then
    read -r -p "Obsidian vault folder on this machine [$DEFAULT]: " VAULT_DIR || true
  fi
  VAULT_DIR="${VAULT_DIR:-$DEFAULT}"
fi
VAULT_DIR="${VAULT_DIR/#\~/$HOME}"
mkdir -p "$VAULT_DIR" "$HOME/voice-rec/models"
put OBSIDIAN_VAULT_DIR "$VAULT_DIR" "$ENVFILE"
echo "Vault: $VAULT_DIR (notes go in 'Voice Notes/')"

if [ -n "${USE_GPU:-}" ] && command -v nvidia-smi >/dev/null && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -n1) — Whisper runs on it"
  put VOICE_REC_GPU 1 "$ENVFILE"
  put COMPOSE_FILE "compose.yaml:compose.gpu.yaml" "$ENVFILE"
  put WHISPER_MODEL "${WHISPER_MODEL:-$(get WHISPER_MODEL "$ENVFILE" | grep . || echo large-v3-turbo)}" "$ENVFILE"
else
  if [ -n "${USE_GPU:-}" ]; then
    echo "USE_GPU is set, but Docker can't use the GPU yet (install nvidia-container-toolkit, see README)."
  fi
  echo "GPU: not used (llama.cpp's model needs it) — Whisper runs on the CPU, a minute or so per note"
  put VOICE_REC_GPU 0 "$ENVFILE"
  put COMPOSE_FILE "compose.yaml" "$ENVFILE"
  put WHISPER_MODEL "${WHISPER_MODEL:-$(get WHISPER_MODEL "$ENVFILE" | grep . || echo small)}" "$ENVFILE"
fi
echo "Whisper model: $(get WHISPER_MODEL "$ENVFILE")"

say "Ports"
cd "$HERE"
RUNNING="$(docker compose ps --status running -q 2>/dev/null || true)"  # voice-rec already up: its ports count as free
PORT="$(pick_port VOICE_REC_PORT $FIRST_PORT)"
DB_PORT="$(pick_port VOICE_REC_DB_PORT $FIRST_DB_PORT)"
[ "$DB_PORT" = "$PORT" ] && DB_PORT="$(pick_port VOICE_REC_DB_PORT $((PORT + 1)))"
put VOICE_REC_PORT "$PORT" "$ENVFILE"
put VOICE_REC_DB_PORT "$DB_PORT" "$ENVFILE"
echo "Server on localhost:$PORT, database on localhost:$DB_PORT"

say "Building and starting (first time: a few minutes)"
docker compose up -d --build

say "Waiting for the server"
for i in $(seq 1 60); do
  if curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 2
  [ "$i" = 60 ] && { docker compose logs --tail 50 voice-rec; die "The server didn't come up; the log is above"; }
done
curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health"; echo

URL="http://127.0.0.1:$PORT"
if [ -z "${NO_SERVE:-}" ] && command -v tailscale >/dev/null; then
  say "Tailscale Serve (HTTPS, your own devices only)"
  NAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' || true)"
  TAKEN=" $(serve_taken "$PORT") "
  HTTPS_PORT="$(get VOICE_REC_HTTPS_PORT "$ENVFILE")"; HTTPS_PORT="${HTTPS_PORT:-$FIRST_HTTPS}"
  while [[ "$TAKEN" == *" $HTTPS_PORT "* ]]; do HTTPS_PORT=$((HTTPS_PORT + 1)); done
  put VOICE_REC_HTTPS_PORT "$HTTPS_PORT" "$ENVFILE"
  if sudo tailscale serve status 2>/dev/null | grep -q ":$HTTPS_PORT"; then
    echo "Already served on port $HTTPS_PORT"
  else
    sudo tailscale serve --bg --https=$HTTPS_PORT "http://127.0.0.1:$PORT" \
      || echo "Couldn't set up Tailscale Serve. If it says HTTPS isn't enabled: Tailscale admin console → DNS → enable MagicDNS and HTTPS, then run this again."
  fi
  [ -n "$NAME" ] && URL="https://$NAME:$HTTPS_PORT"
fi

say "Done. In TaskDeck: Voice notes → tune icon (Settings)"
cat <<EOF
  Workstation address:  $URL
  Access token:         $TOKEN

Then tap "Test connection". The first note takes longer: the Whisper model downloads once.
Browse the notes:       $URL/?token=$TOKEN
Watch it work:          cd $HERE && docker compose logs -f voice-rec
Update later:           cd $(dirname "$HERE") && git pull && bash voice-rec/setup.sh
EOF
