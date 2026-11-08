#!/usr/bin/env bash
# Run Healthbox on this laptop (real box and cameras) with a public https link through a Cloudflare quick tunnel.
# Set the hardware first, for example:
#   HARDWARE_MODE=serial SERIAL_PORT=/dev/cu.usbserial-110 CAMERA_MODE=single_laptop scripts/run_public_link.sh
# Starts from its own data folder (data/live-demo), so the normal data/ is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8020}"
DATA_DIR="${DATA_DIR:-$PWD/data/live-demo}"
command -v cloudflared >/dev/null || { echo "Install the tunnel first: brew install cloudflared"; exit 1; }
log="$(mktemp)"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$log" 2>&1 &
tunnel=$!
trap 'kill "$tunnel" 2>/dev/null || true; rm -f "$log"' EXIT
url=""
for _ in $(seq 1 60); do
  url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -1 || true)"
  [ -n "$url" ] && break
  sleep 1
done
[ -n "$url" ] || { echo "The tunnel did not start:"; tail -5 "$log"; exit 1; }
code="$(openssl rand -hex 4)"
echo
echo "  Public link:  $url"
echo "  Setup code:   $code   (needed once, to create the account over the link)"
echo "  This laptop:  http://127.0.0.1:$PORT"
echo
PORT="$PORT" DATA_DIR="$DATA_DIR" REMOTE_HOST="${url#https://}" REMOTE_SETUP_CODE="$code" \
  caffeinate -dims .venv/bin/python -m app.main
