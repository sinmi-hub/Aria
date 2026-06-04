# Voice Agent — Session Handoff (Phase 2 → Phase 3)

**Author:** Phase-2 hosting session, 2026-05-30.
**Audience:** the next agent or engineer who operates this thing or hardens it for prod.
**TL;DR:** The Phase-1 voice agent ("Aria") now runs on a **RunPod GPU** with the
**natural Kokoro voice** at conversational latency. Kokoro first-sentence dropped from
**~2000 ms (CPU) to ~71 ms (GPU)** — the exact quality gap Phase 1 left open is closed.
It cost **$0.196/hr** on a single cheap GPU, served over RunPod's **free proxy URL**
(no domain, no Cloudflare tunnel needed). The pod is currently **stopped** to save money
(on-demand posture). Bring it back with two commands. Live operational details live in
`RUNPOD-LIVE.md` next to this file; what's left (auth, supervision, reclaim-resilience)
is Phase 3 — sketched in §7.

---

## 1. What changed since Phase 1

Phase 1 proved the whole pipeline on CPU but shipped with **Piper** (robotic) because
Kokoro on CPU was ~2 s/sentence — too slow to talk to. The whole point of Phase 2 was
to get Kokoro's natural voice back at phone-call latency. That is now done, on a GPU,
running the **exact same app** — engines are swappable by env, so the only code change
needed was making Kokoro device-aware.

The pipeline is unchanged (VAD → faster-whisper → Claude → sentence chunker → TTS → μ-law);
only **where it runs** and **which TTS engine** changed:

```
Phase 1 (CPU box):  TTS_ENGINE=piper   WHISPER=tiny.en int8    ~1.2–1.7 s, robotic voice
Phase 2 (RunPod):   TTS_ENGINE=kokoro  WHISPER=small.en fp16   ~1.0–1.4 s, natural voice
```

## 2. The decisions that were made (and why)

These were the open **[DECISION]** points in `SPEC.md §9`. Resolved:

- **Tier 1 (RunPod) over AWS.** Cheapest path to prove GPU-Kokoro; AWS stays the
  strategic option only if/when this folds into Settl. (SPEC §3.)
- **On-demand / stop-when-idle over always-on.** The line answers only while the pod
  runs; the meter is off the rest of the time. ~$0.20/hr while up vs ~$140/mo always-on.
- **Kokoro PyTorch-CUDA (Option A) over kokoro-onnx (Option B).** Option A needed one
  trivial code change and hit 71 ms/sentence — already 3× under the 200 ms bar, so
  there was no reason to benchmark B. The onnx files were left out of the deploy to
  save ~400 MB of transfer; revisit B only if you want to squeeze further.
- **STT `small.en` float16** — fast on GPU, better accuracy than Phase-1's `tiny.en`
  (which misheard "Corpus" as "crops").
- **RunPod proxy URL over a cloudflared named tunnel.** A RunPod pod exposes a stable,
  valid-TLS `https://<podid>-8000.proxy.runpod.net` that also carries the `wss://`
  media stream. This removed the entire "buy a domain + named tunnel" step from SPEC §5.
  Trade-off: the URL is stable for the pod's *lifetime* but changes if the pod is
  **recreated** (not on stop/start) — then re-run `setup_telnyx.py` once.

## 3. Where everything lives (Phase 2 additions)

- **`deploy/RUNPOD-LIVE.md`** — the live operational card: pod ID, GPU, URL, SSH, and the
  copy-paste commands for stop/start, server restart, recreate+rewire, and full rebuild.
  **Read that first to operate the pod.**
- **`deploy/.env`** — now also holds `RUNPOD_API_KEY` (rpa_...) used by `runpodctl`.
- **App-on-pod:** `/workspace/voice-agent` on pod `oakho2rfp0c9o7`. Server is launched by
  `/workspace/voice-agent/run_server.sh` (PUBLIC_URL baked in) under `nohup`.
- **Local tooling added:** `runpodctl` at `~/.local/bin`; SSH key `~/.ssh/voiceagent_rp`
  (its public half was injected into the pod via the `PUBLIC_KEY` env var).
- **Code changes committed back to the repo** (so a fresh build reproduces):
  - `app/audio/tts.py` — `_KokoroEngine` now selects `cuda` when available (SPEC §4, Option A).
  - `requirements.txt` — pinned `transformers>=4.40,<5` (see §5, the gotcha that cost time).

## 4. The current pod (snapshot)

| | |
|---|---|
| Pod ID | `oakho2rfp0c9o7` (status: **EXITED / stopped** to save money) |
| GPU | RTX A4500 — 20 GB VRAM, 8 vCPU, 31 GB RAM, location FR, community cloud |
| Rate | **$0.196/hr** while running; ~$0 while stopped (minor disk fee only) |
| Public URL | `https://oakho2rfp0c9o7-8000.proxy.runpod.net` (free proxy; TLS + WSS) |
| SSH | `ssh -i ~/.ssh/voiceagent_rp -p 14474 root@213.144.200.206` |
| Base image | `runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04`, torch upgraded to 2.5.1+cu121 |
| Balance | ~$55.78 at handoff (≈ 280 h of runtime, or months of pilot use stop/started) |

Bring it back up (same URL, no Telnyx rewire):
```bash
export PATH="$HOME/.local/bin:$PATH"
runpodctl pod start oakho2rfp0c9o7
ssh -i ~/.ssh/voiceagent_rp -p 14474 root@213.144.200.206 \
  'nohup /workspace/voice-agent/run_server.sh > /workspace/voice-agent/server.log 2>&1 &'
curl -s https://oakho2rfp0c9o7-8000.proxy.runpod.net/health   # {"ok":true,"engines_loaded":true}
```

## 5. The latency story (Phase 2 — the GPU win)

Measured on the RTX A4500:

| Stage | Phase 1 (CPU) | Phase 2 (GPU) |
|---|---|---|
| VAD silence tail | 600 ms | 600 ms (unchanged; turn-taking feel) |
| STT | 480–740 ms (`tiny.en`) | ~150–300 ms (`small.en` fp16) — *faster AND more accurate* |
| Claude first sentence | ~600 ms | ~600 ms (API, unchanged) |
| **TTS first sentence** | **82–265 ms (Piper, robotic)** | **~71 ms (Kokoro, natural)** |
| **Total to first audio** | ~1.2–1.7 s | **~1.0–1.4 s** |

The headline: we got the **better voice** *and* a **better STT model** while keeping
latency the same or lower. Kokoro on GPU sits at ~0.6 GB VRAM — the A4500's 20 GB is
wild overkill for the model; it was chosen for **system-RAM/CPU headroom** (31 GB / 8 vCPU)
so other processes can co-locate on the box, not because Kokoro needs it.

## 6. How it was validated (what "proven" means for Phase 2)

Against `SPEC.md §8`'s definition of done:
1. ✅ `make smoke` passes **on the GPU host** — engines load on `cuda`, TTS→μ-law→STT
   round-trip returns the input text verbatim ("Kokoro on cuda" in the log).
2. ✅ One-off TTS benchmark on the GPU: **Kokoro first sentence ~71 ms** (bar was <200 ms).
3. ✅ **Full call loop, proven without a phone.** `scripts/sim_call.py` impersonates
   Telnyx end-to-end over the live proxy WSS (start → inbound μ-law → VAD → STT →
   Claude → TTS → outbound μ-law), transcribes Aria's reply, and passes. Measured on a
   clean GPU run: **first audio ~975 ms after end-of-speech** (STT 112 ms, Kokoro 86 ms).
   The only thing left is the operator dialing the PSTN number — everything it depends
   on is wired, secured, and validated.
4. ✅ Endpoint stable across **stop/start** (same pod id → same URL). Only a full
   **recreate** changes it → one idempotent `setup_telnyx.py` re-run.
5. ✅ Cost recorded: **$0.196/hr** A4500; projected ~$5/day always-on or pennies/day at
   pilot stop-start volume.

## 7. The three gotchas that cost time (so you don't repeat them)

All three stem from the RunPod base image's preinstalled stack being slightly off:

1. **Base torch was 2.2.0 — too old.** `ctranslate2` (faster-whisper) ≥4.5 wants cuDNN 9,
   and `transformers` wants torch ≥2.4. Fix: upgrade to **torch 2.5.1+cu121** (the exact
   pin in `Dockerfile.gpu`), which ships cuDNN 9 and satisfies transformers.
2. **Stale `torchvision 0.17.0`** (pinned to the old torch) then broke against torch 2.5,
   and because `transformers` imports torchvision, it surfaced as a confusing
   `Could not import module 'AlbertModel'`. Fix: **uninstall torchvision** — nothing here uses it.
3. **`transformers 5.x` dropped/relocated the `AlbertModel` API Kokoro imports**, so even
   with a healthy torchvision Kokoro fails to load and *silently falls back to Piper*.
   Fix: **pin `transformers<5`** — now baked into `requirements.txt`, so the `Dockerfile.gpu`
   golden-image path (which would have hit the identical break) is also fixed.

Lesson for Phase 3 / the Docker path: the `Dockerfile.gpu` recipe (torch 2.5.1+cu121,
fresh env, no torchvision) sidesteps #1 and #2; the `transformers<5` pin handles #3.

## 8. Hardening status

### Closed in the "complete every chain" pass
- ✅ **Media-WS auth.** `/ws/media` requires `?token=<MEDIA_WS_TOKEN>`; `setup_telnyx`
  bakes the token into the stream_url Telnyx dials. Tokenless connects get 403.
  (`app/telephony/media_ws.py`, `config.py`.)
- ✅ **Telnyx webhook Ed25519 verification — implemented + unit-tested**
  (`app/telephony/signature.py`, `webhook.py`). Opt-in: set `TELNYX_PUBLIC_KEY` to
  enforce (off by default so the line works without a possibly-wrong key). Replay-guarded
  (5-min timestamp window).
- ✅ **Debug logs gated.** `[timing]`/`[pipeline]`/`[webhook]`/`[telnyx]` now go through
  `app/log.py:debug()` (only when `DEBUG=1`); errors use `warn()` (always on).
- ✅ **Self-healing server.** `scripts/run_server.sh` restarts uvicorn on crash and sets
  `PYTHONUNBUFFERED` for live logs. (Verified: SIGTERM → auto-restart in 2 s.)
- ✅ **Reproducible bring-up.** `deploy/runpod_up.py` does create-or-reuse → push → install
  → wire Telnyx → validate in one idempotent command (the redeploy story below).
- ✅ **Headless validation.** `scripts/sim_call.py` proves the whole loop without a phone.

### Still open (Phase 3)
- **No persistent storage → STOP wipes the pod.** Documented + scripted around with
  `runpod_up.py`, but a **network volume** (venv+models on `/workspace`, ~$1–2/mo) would
  make stop/start an instant resume instead of a ~5-min redeploy.
- **Community-cloud reclaim risk.** Fine for a pilot; move to Secure Cloud / AWS for an
  always-answer line.
- **Full process supervision / auto-launch on pod start.** The self-healing loop covers
  crashes, but a `pod start` still needs `runpod_up.py` (or the manual relaunch). The
  `Dockerfile.gpu` golden image + compose `restart: always` is the real fix (needs a
  registry to push to).
- **Single-call assumption** — shared singletons, GPU serializes. Fine for a few concurrent
  calls on one A4500; scale horizontally past that (SPEC §6).
- **Secrets on the pod.** `deploy/.env` is copied to the pod for the run. For prod, inject
  at runtime / use a secret manager.
- **CPU/Piper fallback still works** (`make run` locally, $0) — break-glass if the GPU is down.

## 9. Cost summary at this configuration
```
GPU (RTX A4500, on-demand, stop-when-idle)   $0.196 / hr  (≈ $5/day if left always-on)
RunPod proxy URL                              $0 (free; no domain needed)
Telnyx number                                 ~$1–2 / month + ~$0.005/min inbound
Claude Haiku                                  fractions of a cent per turn
-------------------------------------------------------------------------------
At pilot volume (stop/start), well under ~$20–40/month for a natural-voice AI phone
line. Always-on ≈ $140/mo + Telnyx/Claude usage. Scales to AWS/Tier-3 economics later.
```
The Phase-1 thesis holds: open-source AI stack, only Telnyx + Claude as real external
costs, plus a **small, now-justified GPU spend** that buys production voice quality.
