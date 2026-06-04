#!/usr/bin/env bash
# Full local run: open a public tunnel, wire Telnyx to it, start the server.
# Call the number printed by setup_telnyx to talk to Aria.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=.venv
PORT="${PORT:-8000}"
TUNNEL_LOG="cloudflared.log"

[ -f .env ] && set -a && . ./.env && set +a

if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "venv missing — run 'make install' first" >&2
  exit 1
fi

cleanup() { kill "${TUNNEL_PID:-}" "${SERVER_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# 1) Public tunnel -> localhost:PORT
echo "Starting cloudflared tunnel..."
: > "$TUNNEL_LOG"
cloudflared tunnel --no-autoupdate --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

PUBLIC_URL=""
for _ in $(seq 1 30); do
  PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)
  [ -n "$PUBLIC_URL" ] && break
  sleep 1
done
if [ -z "$PUBLIC_URL" ]; then
  echo "Failed to get tunnel URL. See $TUNNEL_LOG" >&2
  exit 1
fi
export PUBLIC_URL
echo "Tunnel: $PUBLIC_URL"

# 2) Start the server (loads models — may take ~30s on first run)
echo "Starting server..."
"$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT" &
SERVER_PID=$!

# 3) Wait for health, then wire Telnyx
for _ in $(seq 1 120); do
  if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
"$VENV/bin/python" -m scripts.setup_telnyx

echo ""
echo "Server running. Ctrl-C to stop."
wait "$SERVER_PID"
