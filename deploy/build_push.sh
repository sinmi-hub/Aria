#!/usr/bin/env bash
# Build the golden GPU image and push it to a registry, from the repo root.
# Building is CPU-only here (prefetch runs on CPU); RunPod runs it on a GPU.
#
# Usage:
#   IMAGE=docker.io/<you>/voice-agent:gpu  bash deploy/build_push.sh        # Docker Hub
#   IMAGE=ghcr.io/<you>/voice-agent:gpu    bash deploy/build_push.sh        # GHCR
# Assumes you've already `docker login` (or `docker login ghcr.io`) for that registry.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE:?set IMAGE=<registry>/<repo>:<tag>}"

echo "[build] $IMAGE  (expect ~8-10GB; first build pulls CUDA base + torch + bakes medium.en)"
docker build -f deploy/Dockerfile.gpu -t "$IMAGE" .

echo "[push] $IMAGE"
docker push "$IMAGE"

echo "[done] pushed $IMAGE"
echo "Next: provision a RunPod pod from this image —"
echo "  python deploy/runpod_up.py --image $IMAGE"
