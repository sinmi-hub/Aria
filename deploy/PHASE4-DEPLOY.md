# Phase 4 deploy runbook — rebuild the golden image, deploy for a live inbound call

Goal: get the **current** code (Phase 4: Google Sheets CRM + Calendar + Claude
tool-use + Settl knowledge) onto the RunPod GPU box on the **proven transport** so a
real inbound call reaches Aria with the new brain.

## Why image mode, NOT plain source-push (the trap)
`python deploy/runpod_up.py` (no `--image`) wires Telnyx to the **RunPod HTTP proxy**
(`*-8000.proxy.runpod.net`), which **422s on Telnyx's media WebSocket → the call
answers with no audio**. `sim_call` passes anyway (direct connect), so the deploy
reports green while the live call is silent (RUNPOD-LIVE.md:16-19). The proven
transport is the golden image's built-in **cloudflared tunnel** (`scripts/entrypoint.sh`),
reached via `--image`. So we REBUILD the image (to include Phase 4) and deploy with
`--image`.

## What's already prepared (done — don't redo)
- `requirements.txt` includes the Google libs → baked by the Dockerfile's `pip install`.
- Dockerfile `COPY . .` bakes the current Phase-4 code; CMD is `entrypoint.sh` (cloudflared).
- **`.dockerignore` excludes `.keys/`** → the service-account key is NEVER baked into
  the (public) image.
- `deploy/.env` holds the 7 Phase-4 vars; `_pod_env()` now injects them into the pod.
- `runpod_up.py` `push_key()` copies the key onto the pod at runtime in image mode.

## Steps
1. **Build the image with Phase 4 baked** (from repo root). Use a NEW tag so the proven
   image isn't clobbered until this is validated:
   ```
   docker build -f deploy/Dockerfile.gpu -t docker.io/settl/voice-agent:gpu-phase4 .
   docker push docker.io/settl/voice-agent:gpu-phase4
   ```
   (Changing requirements.txt invalidates the deps + model-prefetch layers, so expect a
   real build + a multi-GB push. Needs Docker + docker.io/settl push creds.)
2. **Bring it up on the new image** (creates pod, entrypoint tunnels + self-wires
   Telnyx, pushes the key, validates with sim_call against the tunnel URL):
   ```
   python deploy/runpod_up.py --image docker.io/settl/voice-agent:gpu-phase4
   ```

## Phase-4 post-deploy checks
The script prints `public_url` (the trycloudflare URL) and the Telnyx number. Then:
1. **Config reached the container:** open `https://<podid>-8000.proxy.runpod.net/debug/config`
   — confirm `public_url` is a `trycloudflare.com` URL (NOT the runpod proxy).
2. **Key + tools live (not text-only):** SSH in (script prints ip/port; key
   `~/.ssh/voiceagent_rp`) and run:
   ```
   ls -l /app/.keys/settl-voice-agent-d9eb6f473afe.json
   cd /app && python3 -c "from app.integrations.crm import SheetsLeadStore; print(SheetsLeadStore().get_lead_by_phone('4439292703').name)"
   ```
   → second line should print `Alex Rivera`. If the key is missing, the agent answers
   but can't look up / book.

## The live call
Call the Telnyx number (`+15551234567` unless changed). `tail -f /app/server.log`
(or the pod logs) to watch `lookup_lead → find_open_slots → book_meeting → log_call`.
After: the test lead's Sheet row shows `demo_booked` + summary; the event appears on the
`Settl Demos` calendar. **No email invite yet** — that needs DWD (see TODO.md), expected.

## Constraints
- Community Cloud only. No git commits/pushes. The key is runtime-only — never baked.
- Once validated, retag/replace `:gpu` with the Phase-4 image so `--image` defaults stay correct.
