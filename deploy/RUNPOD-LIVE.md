# RunPod GPU deployment — operational card (WORKING)

Aria runs on a RunPod GPU from a **golden Docker image that runs its own cloudflared
tunnel**. Proven on a real phone call (natural Kokoro voice, `medium.en`). Call **+15551234567**.

## Architecture (and the key lesson)
The container is fully self-contained and self-configuring:
1. `scripts/entrypoint.sh` (image CMD) starts a **cloudflared quick tunnel** → gets a
   `https://<random>.trycloudflare.com` URL.
2. It derives `PUBLIC_URL` from that, launches uvicorn, and runs `scripts/setup_telnyx`
   to point Telnyx's webhook + media stream at the tunnel — **self-wiring on every boot**.
3. Supervises uvicorn (restarts on crash).

**Why cloudflared and not RunPod's proxy:** RunPod's HTTP proxy (`*-8000.proxy.runpod.net`)
is **unreliable for Telnyx's media WebSocket** — `streaming_start` returns
`422 "Failed to connect to destination"` (call answers, but no audio). HTTP/`/health`
works fine through it, which is why `sim_call` (direct connect) passed while real calls
failed. cloudflared is the Phase-1-proven endpoint Telnyx connects to reliably. The
RunPod proxy is now used **only** for my external `/health` + `/debug/config` reads.

## Current pod
**NO LIVE POD — the realtime sales line (+15551234567) is OFFLINE.** Pod `uf7hn277cjski2`
(`gpu-phase7b`) was **deleted 2026-06-02 at session end** to stop the meter (key rotated,
Phase 7 parked, nothing pending). The line stays down until the next bring-up.

| | |
|---|---|
| Latest production image | `docker.io/settl/voice-agent:gpu-phase7b` (OpenAI realtime backend + `marin`; also carries the Phase-7 SLM-probe deps + `/debug/slm_probe`, harmless when idle). Overlay on `gpu-phase6d`. |
| Pod | none (deleted) |

**Bring it back (one command — self-wires Telnyx + realtime from `deploy/.env`):**
```bash
cd ~/voice-agent && export PATH="$HOME/.local/bin:$PATH"
python deploy/runpod_up.py --image docker.io/settl/voice-agent:gpu-phase7b --no-validate
# then verify: curl https://<newpodid>-8000.proxy.runpod.net/health   (and /debug/config for the tunnel URL)
```
First boot pulls the ~19GB image on a cold community host (5–10 min); the entrypoint makes a new
cloudflared tunnel and re-points Telnyx itself. (`runpod_up` reuses an existing `voice-agent` pod
if one is running, so to swap images: `runpodctl pod delete <id>` first.)

## Bring it up (one command)
```bash
cd ~/voice-agent
python deploy/runpod_up.py --image docker.io/settl/voice-agent:gpu
```
Creates a pod from the image; the entrypoint tunnels + self-wires Telnyx; the script
reads `/debug/config` for the real `media_ws_url` and validates the full loop with
`sim_call`. First boot pulls the ~7 GB image (5–10 min on a cold host).

## Stop / resume
```bash
export PATH="$HOME/.local/bin:$PATH"
runpodctl pod stop  7hxwk5qhkmfq3j     # halt the meter ($0 while stopped)
runpodctl pod start 7hxwk5qhkmfq3j     # entrypoint makes a NEW tunnel + re-wires Telnyx automatically
```
On start the trycloudflare URL changes, but the entrypoint re-wires Telnyx itself, so
no manual step. (A cold start may re-pull the image.) If a start ever comes up wrong,
`runpodctl pod delete …` then re-run `runpod_up.py --image …`.

## Verify it's really working (before a call)
```bash
P=https://7hxwk5qhkmfq3j-8000.proxy.runpod.net
curl -s $P/health                       # {"ok":true,...}
curl -s $P/debug/config                  # public_url must be a trycloudflare URL (NOT empty/proxy)
MWS=$(curl -s $P/debug/config | python3 -c 'import sys,json;print(json.load(sys.stdin)["media_ws_url"])')
python -m scripts.sim_call "$MWS" --say "what are your hours?"   # full-loop check over the tunnel
# and confirm Telnyx points at the tunnel:
#   the call-control app 'voice-agent-aria' webhook_event_url should be the trycloudflare URL
```

## Rebuild the image (after code changes)
```bash
docker build -f deploy/Dockerfile.gpu -t docker.io/settl/voice-agent:gpu .   # CPU build OK
# push via Docker Desktop GUI (WSL shell can't reach the cred helper): Images -> Push to Hub
python deploy/runpod_up.py --image docker.io/settl/voice-agent:gpu           # recreate
```
Verify the entrypoint locally before pushing (no GPU/Telnyx needed):
```bash
docker run -d --name va-test -p 8010:8000 -e WHISPER_DEVICE=cpu -e WHISPER_COMPUTE=int8 \
  -e MEDIA_WS_TOKEN=localtest docker.io/settl/voice-agent:gpu
curl -s localhost:8010/debug/config   # public_url should be a trycloudflare URL
docker rm -f va-test
```
(Local CPU note: override `WHISPER_COMPUTE=int8` — `float16` is GPU-only.)

## Config / secrets (deploy/.env, gitignored)
`RUNPOD_API_KEY`, `MEDIA_WS_TOKEN` (enforced on `/ws/media`), `WHISPER_MODEL=medium.en`,
`TELNYX_PUBLIC_KEY` (blank = webhook signature verification off). `runpod_up.py --image`
injects secrets + token as pod env; `PUBLIC_URL` is NOT set (the tunnel provides it).

## Cost
~$0.196/hr running; ~$0 stopped. Balance ~$55. `runpodctl user`.

## Still open (optional, Phase 3+)
- Image is public; to go private, `runpodctl registry create` + `--registry-auth-id` at create.
- cloudflared **named** tunnel (stable URL) instead of quick tunnel — needs a Cloudflare domain.
- Secure Cloud / always-on for an SLA line (PHASE3-DECISIONS.md).
