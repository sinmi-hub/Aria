#!/usr/bin/env bash
# Self-healing server launcher. Restarts uvicorn if it ever exits, so a transient
# crash doesn't take the line down (poor-man's supervision for the marketplace pod;
# the Dockerfile.gpu + compose `restart: always` path is the full-fat version).
#
# Reads .env (so PUBLIC_URL / secrets are picked up). Run under nohup:
#   nohup scripts/run_server.sh > server.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${PORT:=8000}"
export PYTHONUNBUFFERED=1   # flush [timing]/[pipeline] logs to server.log in real time

while true; do
  echo "[run_server] starting uvicorn ($(date -u +%FT%TZ)) PUBLIC_URL=${PUBLIC_URL:-<unset>}"
  python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
  code=$?
  echo "[run_server] uvicorn exited (code=$code); restarting in 2s"
  sleep 2
done
