# Phase 2 Spec — Host the Voice Agent on GPU for Kokoro-quality voice

**Status:** proposed, ready to execute.
**Prereq:** Phase 1 complete and proven (see `SESSION-HANDOFF.md`).
**One-line goal:** run the *exact same app* on a GPU host so Kokoro TTS gives a
natural voice at conversational latency (~1–1.5s), at the **minimum hosting cost
that meets that bar** — then scale cost only as call volume justifies it.

This spec is written so it can be executed hands-off. It states the decisions, the
ladder of options with real prices, the concrete code/infra changes, and a
validation checklist. Where a human choice is genuinely required, it is marked
**[DECISION]**.

---

## 1. Why GPU (settled, with data)

Benchmarked on the Phase-1 CPU box (10-core, WSL2):

| TTS option (CPU) | Latency/sentence | Voice quality |
|---|---|---|
| Piper (current) | 100–300 ms | robotic — rejected by user |
| Kokoro PyTorch | ~2000 ms | natural — too slow |
| Kokoro ONNX fp32 | 1000–2000 ms (0.6× RT) | natural — too slow |
| Kokoro ONNX int8 | 2600–5100 ms (1.5× RT) | natural — slower, rejected |

On a GPU (T4 / RTX 3060 / A10 class), Kokoro-82M runs ~10–50× real-time
(~50–150 ms/sentence). It is a *tiny* model (<2 GB VRAM) — **you do not need an
expensive GPU.** The cheapest available GPU clears the bar. That is the whole
cost argument: a small fixed GPU spend buys both quality and speed.

## 2. Target latency budget on GPU
```
VAD tail        500–600 ms   (tunable; turn-taking feel)
STT small.en    150–300 ms   (GPU float16)
Claude 1st sent ~600 ms      (API, unchanged)
Kokoro 1st sent  50–150 ms   (GPU)
------------------------------------------------
first audio     ~1.0–1.4 s after caller stops speaking
```
Quality goes up (small.en + Kokoro) *and* latency stays ~same or better than the
Phase-1 CPU/Piper number.

## 3. Hosting cost ladder  [DECISION: pick a tier]

Prices are May 2026 market refs; verify at purchase. All tiers need a GPU with
≥6–8 GB VRAM (T4/RTX3060 is ample for Kokoro-82M + whisper-small).

### Tier 1 — Cheap persistent GPU on a GPU marketplace  ← RECOMMENDED START
- **RunPod** (community cloud) or **Vast.ai**.
- Refs: Vast.ai RTX 3090 ~$0.16/hr, RTX 4090 ~$0.31/hr; RunPod community
  RTX 4090 ~$0.69/hr (RunPod = more reliable, Vast = cheaper P2P, can be preempted).
- A **T4 / RTX 3060** tier is plenty and often ~$0.15–0.25/hr.
- **Always-on:** ~$110–230/month. **Business-hours only (e.g. 10h/day):** ~$45–80/mo.
- RunPod **network volumes** let you stop the GPU pod (stop paying for GPU) while
  keeping models on disk for pennies — good for on-demand/testing.
- **Pros:** cheapest path to GPU Kokoro; simple Docker deploy. **Cons:** marketplace
  reliability (mitigate with RunPod over Vast for prod); you manage uptime.

### Tier 2 — Serverless GPU, scale-to-zero (Modal / RunPod Serverless / Beam)
- Per-second billing; pay only while a call is active.
- **Problem:** an inbound call must be answered in <2s, but a cold container
  (load CUDA + models) takes ~10–30s. Scale-to-zero ⇒ first call after idle is bad.
- **Mitigations:** keep `min_containers=1` warm (≈ always-on cost again), OR answer
  immediately and play a "one moment…" line while the worker warms.
- **Verdict:** great once call volume is bursty/high; not cheaper than Tier 1 for
  steady low volume. Revisit at scale.

### Tier 3 — Managed cloud GPU (AWS g4dn/g5, GCP)  ← STRATEGIC, not cheapest
- AWS `g4dn.xlarge` (T4) ~$0.526/hr on-demand (~$380/mo); spot ~1/3 of that.
- **Why consider it anyway:** Settl already runs on AWS. If this agent becomes part
  of Settl, co-locating (same VPC, IAM, secrets, observability) may beat a few
  dollars of marketplace savings. Use **spot** + autoscaling to control cost.

**Recommendation:** start **Tier 1 on RunPod** (T4/RTX3060, network volume) to prove
GPU-Kokoro quality + latency cheaply. Move to Tier 3 (AWS) only if/when it must
integrate with Settl or needs enterprise reliability.

## 4. Code changes (small — the app is already GPU-ready by config)

The pipeline, telephony, codec, VAD, STT, sentence chunker need **no changes**.
Only TTS needs a GPU-aware Kokoro path. Two options:

**Option A (simplest): PyTorch Kokoro on CUDA.** `app/audio/tts.py` already uses
`KPipeline`. Make it device-aware:
```python
# in _KokoroEngine.__init__
import torch
device = "cuda" if torch.cuda.is_available() else "cpu"
self.pipeline = KPipeline(lang_code=settings.kokoro_lang, device=device)
```
Then `make install` must install the **CUDA** torch wheel (not the CPU pin).

**Option B (often faster): `kokoro-onnx` with CUDAExecutionProvider.** Add a
`_KokoroOnnxEngine` selected by `TTS_ENGINE=kokoro_onnx`, using the model files
already downloaded to `voices/kokoro-v1.0.onnx` + `voices/voices-v1.0.bin` and
`providers=["CUDAExecutionProvider"]`. Benchmark A vs B on the chosen GPU; keep both.

STT GPU is pure config: `WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE=float16`,
`WHISPER_MODEL=small.en` (already in `deploy/.env`).

**[DECISION]** Option A vs B — pick after a one-off benchmark on the target GPU.

## 5. Stable public endpoint (replace the ephemeral quick tunnel)

Telnyx requires a valid-TLS, internet-reachable `wss://` for media and an HTTPS
webhook. Quick tunnels are random per run; prod needs stable.

**Chosen: RunPod (no cloudflared, no domain).** RunPod exposes a pod's port via its
own proxy with valid TLS — `https://{POD_ID}-8000.proxy.runpod.net`. Use that as
`PUBLIC_URL`; `config.py` derives the webhook (`/telnyx/webhook`) and media
(`wss://…/ws/media`) from it. This is *why* RunPod was picked: it removes the tunnel
+ domain step entirely. RunPod is also container-native, so the Phase-2 golden Docker
image (§7, §11) deploys directly. `RUNPOD_API_KEY` is in `deploy/.env` (for the
RunPod API/CLI to create the pod). The pod proxy URL is stable for the pod's life;
if you recreate the pod, re-run `scripts/setup_telnyx.py` with the new `PUBLIC_URL`.

Fallback options (if not on RunPod) — both still valid:

- **cloudflared *named* tunnel (free, recommended):** needs a Cloudflare account +
  a domain on Cloudflare. Gives a stable hostname (e.g. `voice.yourdomain.com`),
  valid TLS, no open inbound ports, works behind NAT/marketplace GPUs. See
  `cloudflared-named-tunnel.md`. `scripts/start.sh` then uses a fixed
  `PUBLIC_URL` instead of scraping a quick-tunnel URL.
- **Host domain + Caddy** (auto Let's Encrypt) if the GPU host has a public IP and
  you can open :443.

Then set the Telnyx Call Control app's `webhook_event_url` once to the stable URL
(via `scripts/setup_telnyx.py`, which becomes a one-time step, not per-run).

## 6. Concurrency & scaling
- Engines are loaded once and shared; GPU inference serializes naturally. A single
  T4 comfortably handles a handful of concurrent calls (STT+TTS are short bursts).
- Run **uvicorn with 1 worker** (multiple workers = multiple model copies = wasted
  VRAM). Scale **horizontally** (more pods behind Telnyx number pools / a load
  balancer) when concurrent calls exceed one GPU's headroom.
- Add a max-concurrent-calls guard; reject/queue beyond capacity gracefully.

## 7. Productionization checklist
- [ ] **Deploy method — Docker (chosen).** Build the self-contained golden image
      `deploy/Dockerfile.gpu` (CUDA + torch pinned + system libs + model weights all
      baked in), run it with `deploy/docker-compose.gpu.yml`. Secrets injected at
      runtime via `--env-file`, never baked. Bare-metal + systemd
      (`bare-metal-runbook.md`) remains a documented alternative. See §11 for why.
- [ ] Secrets via host env / secret manager (never in the image). `deploy/.env`
      shows the required vars.
- [ ] Gate debug logs (`[timing]`/`[pipeline]`) behind a `DEBUG`/log-level flag.
- [ ] Telnyx **webhook signature verification** in `app/telephony/webhook.py`
      (Ed25519, `telnyx-signature-ed25519` + timestamp headers).
- [ ] Restrict / authenticate the media WS (path token or Telnyx IP allowlist).
- [ ] Process supervision: Docker `restart: always` or systemd; health probe on
      `/health` (already exists).
- [ ] Basic metrics/logging: per-turn latency, call count, errors.
- [ ] Cost guardrail: auto-stop/scale-down schedule if running marketplace GPU.

## 8. Validation plan (definition of done for Phase 2)
1. `make smoke` passes on the GPU host (engines load on CUDA).
2. One-off TTS benchmark on the GPU: Kokoro first sentence < ~200 ms.
3. Live call to `+15551234567`: natural Kokoro voice, **first audio < ~1.5 s**
   after end-of-speech, barge-in still works.
4. Endpoint is stable across restarts (named tunnel / domain) — no per-run rewiring.
5. Cost recorded: $/hr of chosen GPU + projected monthly at expected call hours.

## 9. Open decisions for the operator
- **[DECISION] Tier 1 (RunPod/Vast) vs Tier 3 (AWS).** Default: Tier 1 RunPod to
  prove cheaply; AWS only for Settl integration / enterprise needs.
- **[DECISION] Always-on vs business-hours/on-demand GPU.** Drives monthly cost
  (~$150–230 vs ~$45–80). Depends on whether the line must always answer.
- **[DECISION] Kokoro PyTorch-CUDA (Option A) vs kokoro-onnx-GPU (Option B).**
  Decide by benchmark on the chosen GPU.
- **[DECISION] STT accuracy:** `small.en` (recommended on GPU — fast there) vs
  `medium.en` if accuracy needs lifting.

## 10. Cost summary at the recommended start (Tier 1, RunPod, business-hours)
```
GPU (T4/RTX3060, ~10h/day)   ~$45–80 / month
cloudflared named tunnel      $0 (free; needs a domain on Cloudflare)
Telnyx number                 ~$1–2 / month + ~$0.005/min inbound
Claude Haiku                  fractions of a cent per turn
------------------------------------------------------------------
≈ under ~$80/month to run a natural-voice AI phone line at pilot volume,
  scaling to ~$150–230/mo always-on, or AWS/Tier-3 economics at scale.
```
This preserves the project thesis: open-source AI stack, only Telnyx + Claude as
real external costs, and now a *small, justified* GPU spend buys production voice
quality.

## 11. Docker vs. bare-metal — why, and which to choose here

**What Docker buys you (and why it appears in voice/AI stacks):**
- Pins the *system* layer that is the actual pain in this stack — the CUDA runtime
  ↔ torch version match, plus `espeak-ng`/audio libs. The image freezes a known-good
  combo so it can't drift.
- Portability: GPU marketplaces (RunPod, Vast) are **container-native** — a "pod" is
  a container; you pick a base image. There, you're using Docker whether or not you
  write a Dockerfile.
- Clean teardown / rollback / "works on my machine" elimination.

**What's wrong with hosting directly on the server (bare-metal)? Honestly — nothing,
for a single-service box that one person manages.** It is often *simpler* to operate:
- You SSH in, see real processes, files, and logs directly — no container indirection
  to debug through. For someone who finds Docker daunting, this is a feature.
- The one-time setup (CUDA driver + `make install` with the CUDA torch wheel +
  systemd unit) is a ~20-minute runbook, done once per box.
- Trade-offs vs Docker: environment can drift over months; reproducing on a *new*
  box means re-running the runbook (not just `docker run`); a CUDA/driver mismatch is
  debugged on the host rather than rebuilt. For one long-lived box these are minor.

**Decision (chosen): use Docker.** The deciding reason is reproducibility — the
CUDA↔torch↔espeak-ng combo is exactly the brittle layer Docker freezes, the same
"bake a known-good environment" logic as an AWS **Golden AMI**, but portable across
any host and scoped to this one app. Trade-offs (image size ~6–7 GB; learning curve)
are accepted for a frozen, rebuildable environment.

Build the self-contained golden image with `Dockerfile.gpu` (torch `2.5.1+cu121`
pinned; model weights baked via `scripts/prefetch_models.py` → zero first-call
download) and run it via `docker-compose.gpu.yml`. Host needs the **NVIDIA Container
Toolkit**. Works on any GPU host: a VM you control (Lambda Labs / Paperspace / AWS
g4dn-g5 / Hetzner) or a container-native marketplace (RunPod/Vast). `bare-metal-runbook.md`
stays as the no-Docker fallback if ever needed.
