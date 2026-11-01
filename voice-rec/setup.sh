#!/usr/bin/env bash
# Set up (or update) the voice-rec server on the AI Workstation. Safe to run again: it keeps your
# token, database password and vault choice, rebuilds, and restarts.
#
# From the phone (Termius), paste:
#
#     cd ~/ScratchPatch && git pull && bash voice-rec/setup.sh
#
# Options (environment variables): VAULT=/path/to/vault  NO_SERVE=1 (skip Tailscale Serve)
#                                  WHISPER_MODEL=large-v3-turbo  FORCE_CPU=1
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$HOME/.config/secrets.env"
ENVFILE="$HERE/.env"
PORT=8093
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

if [ -z "${FORCE_CPU:-}" ] && command -v nvidia-smi >/dev/null && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -n1) — Whisper runs on it"
  put VOICE_REC_GPU 1 "$ENVFILE"
  put COMPOSE_FILE "compose.yaml:compose.gpu.yaml" "$ENVFILE"
  put WHISPER_MODEL "${WHISPER_MODEL:-$(get WHISPER_MODEL "$ENVFILE" | grep . || echo large-v3-turbo)}" "$ENVFILE"
else
  if command -v nvidia-smi >/dev/null && [ -z "${FORCE_CPU:-}" ]; then
    echo "There's an NVIDIA GPU, but Docker can't use it yet (install nvidia-container-toolkit, see README)."
  fi
  echo "GPU: not used — Whisper runs on the CPU (slower; fine for short notes)"
  put VOICE_REC_GPU 0 "$ENVFILE"
  put COMPOSE_FILE "compose.yaml" "$ENVFILE"
  put WHISPER_MODEL "${WHISPER_MODEL:-$(get WHISPER_MODEL "$ENVFILE" | grep . || echo small)}" "$ENVFILE"
fi
echo "Whisper model: $(get WHISPER_MODEL "$ENVFILE")"

say "Building and starting (first time: a few minutes)"
cd "$HERE"
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
  if sudo tailscale serve status 2>/dev/null | grep -q ":$PORT"; then
    echo "Already served on port $PORT"
  else
    sudo tailscale serve --bg --https=$PORT "http://127.0.0.1:$PORT" \
      || echo "Couldn't set up Tailscale Serve. If it says HTTPS isn't enabled: Tailscale admin console → DNS → enable MagicDNS and HTTPS, then run this again."
  fi
  [ -n "$NAME" ] && URL="https://$NAME:$PORT"
fi

say "Done. In TaskDeck: Voice notes → tune icon (Settings)"
cat <<EOF
  Workstation address:  $URL
  Access token:         $TOKEN

Then tap "Test connection". The first note takes longer: the Whisper model downloads once.
Browse the notes:       $URL/?token=$TOKEN
Watch it work:          cd $HERE && docker compose logs -f voice-rec
Update later:           cd ~/ScratchPatch && git pull && bash voice-rec/setup.sh
EOF
