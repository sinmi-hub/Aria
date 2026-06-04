# Phase 3 — decisions & rationale

Recorded 2026-05-30. Captures the four hardening options considered and what we
chose, so the reasoning isn't lost. Status legend: ✅ done · ▶ in progress · ⏸ deferred.

## Decision matrix

| Option | Decision | Why | Cost |
|---|---|---|---|
| **Golden Docker image** (`Dockerfile.gpu` + `restart: always`) | ▶ **DO — primary persistence + supervision** | Bakes CUDA+torch+deps+models in → restarted/fresh pod has everything (fast start, no downloads), reproducible, portable. Also the SPEC's chosen approach. Solves "STOP wipes the pod" *and* "no process supervision" in one move. | Needs a container registry (Docker Hub / GHCR) login. ~7 GB image. Build done on the pod (no Docker locally). |
| **Network volume** (venv+models on `/workspace`) | ⏸ **SKIP (redundant)** | Also solves persistence, but the golden image already does — doing both is redundant. Volume also locks us to one datacenter and pairs awkwardly with community GPU availability. Revisit only if we need runtime-mutable state (call recordings, etc.), which we don't today. | ~$1–2/mo storage (avoided). |
| **`medium.en` STT** | ✅ **DONE** | `small.en` mis-heard lossy phone audio ("repeat my papers"). `medium.en` is markedly more accurate; on GPU the extra cost is ~+150–250 ms, leaving first-audio ~1.1–1.3 s — still in target. Set in `deploy/.env`; `prefetch_models.py` bakes it into the image. | Free (~+200 ms latency). Revert by setting `WHISPER_MODEL=small.en`. |
| **Secure Cloud / always-on** | ⏸ **DEFER** | Not needed for a pilot/dev line you stop when idle. Community cloud is cheaper and has been reliable; its only downside (host reclaim) is rare and recovered by one redeploy / image pull. Move to Secure Cloud when the line must answer **24/7 with an SLA** or for compliance. | Higher hourly rate + always-on (~$140/mo) — avoided until production. |

## Net Phase-3 plan (chosen)
1. ✅ `medium.en` for transcription accuracy.
2. ▶ Build the **golden Docker image** on the pod, push to **your registry**, and run it
   via `docker-compose.gpu.yml` with `restart: always`. This gives: persistent (image-baked)
   environment, crash supervision, and zero first-call downloads — superseding both the
   bare `nohup` launcher and the need for a network volume.
3. ⏸ Network volume — skipped (redundant with #2).
4. ⏸ Secure Cloud / always-on — deferred to production cutover.

## Build path (chosen): local Docker Desktop on WSL
There's no Docker on this box and a RunPod pod can't build images (unprivileged). So:
**you install Docker Desktop for Windows + enable WSL integration**, and I build the
image here (CPU build is fine — `prefetch_models.py` runs on CPU; RunPod runs it on the
GPU). Then push to a registry and create a pod *from the image* — no per-boot install.

**Tooling already in place (committed):**
- `deploy/Dockerfile.gpu` — fixed: python3.10 base, bakes `medium.en`, copies `config.py`
  before prefetch, self-healing `run_server.sh` as CMD, no torchvision, secrets never baked.
- `deploy/build_push.sh` — `IMAGE=<registry>/voice-agent:gpu bash deploy/build_push.sh`.
- `deploy/runpod_up.py --image <ref>` — creates a pod from the image (no code push / pip
  install / SSH): injects secrets+token as pod env, the app **self-derives PUBLIC_URL from
  `RUNPOD_POD_ID`** (config.py), the image CMD auto-launches the server, then Telnyx is
  wired from here and validated with `sim_call`.

**What I still need from you:** a registry login the pod can pull from —
- **Docker Hub:** `docker login` (username + access token from hub.docker.com → Account →
  Security), image `docker.io/<you>/voice-agent:gpu`; or
- **GHCR:** `docker login ghcr.io` (GitHub PAT with `write:packages`), image
  `ghcr.io/<you>/voice-agent:gpu`. For RunPod to pull a *private* GHCR image, either make
  the package public or add registry creds to the RunPod pod.

Steps once Docker is up: `docker login …` → `IMAGE=… bash deploy/build_push.sh` →
`python deploy/runpod_up.py --image …`. Bring-up then = image pull (fast), not redeploy.

## Endpoint reality (discovered during deploy — important)
The golden image first used RunPod's HTTP proxy (`*-8000.proxy.runpod.net`) as the
public endpoint. It worked for HTTP/`/health` and for `sim_call` (direct WS connect),
**but Telnyx's media WebSocket failed**: `streaming_start` → `422 "Failed to connect
to destination"` — the call answered but no audio. The proxy is unreliable for the
kind of WS Telnyx opens. **Fix (shipped):** the image runs its OWN **cloudflared quick
tunnel** (`scripts/entrypoint.sh`) — the Phase-1-proven endpoint — derives PUBLIC_URL
from it, and self-wires Telnyx on every boot. Confirmed on a real phone call.
Lesson: never route Telnyx through the RunPod proxy; use cloudflared (or a named
tunnel / Caddy + domain for a stable URL). Added `/debug/config` so the live endpoint
is verifiable remotely (image pods have no SSH).

## Open Phase-3 items still beyond this set (future)
- Auto-launch on `pod start` (compose `restart: always` covers crashes; a stopped→started
  pod still needs `docker compose up` unless we register it as the pod's start command).
- Horizontal scaling past one GPU's concurrent-call headroom (more pods + number pool).
- Runtime secret injection / secret manager (today `deploy/.env` is copied to the pod).
