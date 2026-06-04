#!/usr/bin/env bash
# Golden-image entrypoint. RunPod's HTTP proxy is unreliable for Telnyx's media
# WebSocket (streaming_start -> 422 "Failed to connect to destination"), so the
# container runs its OWN cloudflared quick tunnel — the Phase-1 endpoint Telnyx
# connected to reliably — derives PUBLIC_URL from it, wires Telnyx, and supervises
# uvicorn. Secrets/token come from injected env vars (no .env baked).
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
[ -f .env ] && set -a && . ./.env && set +a    # local runs only; image has none
: "${PORT:=8000}"

# 1) Reliable public endpoint via cloudflared (unless PUBLIC_URL was supplied).
if [ -z "${PUBLIC_URL:-}" ] && command -v cloudflared >/dev/null 2>&1; then
  echo "[entrypoint] starting cloudflared quick tunnel -> localhost:${PORT}"
  cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}" > /tmp/cf.log 2>&1 &
  for _ in $(seq 1 40); do
    PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cf.log | head -1 || true)
    [ -n "${PUBLIC_URL:-}" ] && break
    sleep 1
  done
  export PUBLIC_URL
fi
echo "[entrypoint] PUBLIC_URL=${PUBLIC_URL:-<unset>}"

# 2) Supervise uvicorn; wire Telnyx once the server is healthy (tunnel/URL persist
#    across uvicorn restarts, so wiring is done once).
wired=0
while true; do
  python3 -m uvicorn main:app --host 0.0.0.0 --port "${PORT}" &
  uv=$!
  if [ "$wired" = "0" ]; then
    for _ in $(seq 1 60); do
      curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1 && break
      sleep 1
    done
    if [ -n "${PUBLIC_URL:-}" ] && [ -n "${TELNYX_API:-}" ]; then
      if python3 -m scripts.setup_telnyx; then wired=1; else echo "[entrypoint] telnyx wiring failed; will retry next loop"; fi
    fi
  fi
  wait "$uv"
  echo "[entrypoint] uvicorn exited; restarting in 2s"
  sleep 2
done
