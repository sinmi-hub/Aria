# Voice AI Agent — Architecture Validation

Validated **before** implementation, against the live environment and accounts.
Date: 2026-05-30.

## Credentials (verified live)

| Secret | Source file | Status |
|---|---|---|
| `TELNYX_API` | `~/web-portal/.env` | ✅ HTTP 200 — account active |
| `TELNYX_FROM_NUMBER` = `+18005550100` | `~/web-portal/.env` | toll-free |
| `ANTHROPIC_API_KEY` | `~/agent-orchestrator/.env` | ✅ HTTP 200, `claude-haiku-4-5` resolves |

### Telnyx account state at validation time
- 3 active US numbers, all on a SIP `credential_connection` ("Forward Only"):
  - `+18005550100` (toll-free)
  - `+15551234567` (local)  ← **dedicated to the agent**
  - `+15551239876` (local)
- **0 TeXML apps, 0 Call Control apps existed.** `scripts/setup_telnyx.py` creates a
  Call Control app and reassigns the local number to it.

## Hardware reality (drives model + latency choices)
- WSL2, **CPU-only (no GPU)**, 10 cores, 9.7 GB RAM, Python 3.10.

### Measured latency (live calls, this box)
| Config | STT | Claude 1st sentence | TTS 1st sentence | Total to first audio |
|---|---|---|---|---|
| Kokoro + `small.en` | ~1000ms | ~600ms | **~2000ms** | **~3.4–7s** |
| **Piper + `tiny.en`** (CPU default) | 480–740ms | 600–720ms | **82–265ms** | **~1.2–1.7s** |

Findings:
- **Kokoro is a neural TTS built for GPU** — ~2s/sentence on CPU; the dominant cost.
  **Piper** (also self-hosted, free) does a sentence in ~100–300ms on CPU.
- A latent bug compounded it: `torch.set_num_threads(1)` (set for Silero) throttled
  **all** torch ops incl. Kokoro to one core. Removed.
- Claude API first-token measured at **~520ms** from this box — never the bottleneck.

### Can Kokoro stay on CPU? (tested — no)
Benchmarked `kokoro-onnx` on this CPU to see if a GPU could be avoided:
- fp32 ONNX: ~0.6× real-time (1–2 s/sentence) — too slow.
- int8 ONNX: ~1.5× real-time (slower; int8 not optimized on this CPU).
So Kokoro quality at conversational latency requires a GPU on hardware like this.

### Recommended profiles
- **CPU (here):** `TTS_ENGINE=piper`, `WHISPER_MODEL=tiny.en` (or `base.en` for accuracy).
- **GPU host (next step):** `TTS_ENGINE=kokoro`, `WHISPER_MODEL=small.en`,
  `WHISPER_DEVICE=cuda` — Kokoro quality at low latency. Config flip + one small
  TTS code addition. Full hosting plan: **`deploy/SPEC.md`**; session handoff:
  **`deploy/SESSION-HANDOFF.md`**.

## Decisions locked with the user
1. **Inbound** test: user dials the Telnyx local number → talks to Aria.
2. **cloudflared** quick tunnel exposes `localhost:8000` as a public `wss://`
   (Telnyx cannot reach a home WSL2 box otherwise — the brief's `ws://your-ip:8000`
   does not work from a residential network).
3. **Balanced** profile: faster-whisper `small.en` + Kokoro TTS.

## Telephony path (concrete)
Telnyx **Call Control** (Voice API), bidirectional media streaming:

```
PSTN → Telnyx → POST /telnyx/webhook
  call.initiated  → REST: answer
  call.answered   → REST: streaming_start (stream_url=wss://<tunnel>/ws/media,
                          bidirectional rtp, codec PCMU)
Media (μ-law 8k, base64 JSON frames) ⇄ WS /ws/media
  inbound  frames → decode → pipeline
  outbound frames → TTS μ-law → {"event":"media"} frames
  barge-in        → {"event":"clear"} flushes Telnyx playback buffer
```

Two URLs through one tunnel: `https://…/telnyx/webhook` (HTTP events) and
`wss://…/ws/media` (audio). This is why the brief's single `/ws/call` is split.

## Divergences from the brief (and why)
- **`/ws/call` → `/telnyx/webhook` (HTTP) + `/ws/media` (WS).** Call Control separates
  the event channel from the media channel.
- **`app/` package with `telephony/` subpackage** added on top of the brief's
  `audio/` + `agent/` layout — keeps the Telnyx wire protocol isolated and every
  file < 300 lines, as required.
- **Latency target relaxed** to ~1.5–2.5s on this CPU box (see Hardware reality).

Everything else follows the brief: Silero VAD, faster-whisper, Kokoro (Piper
fallback), Claude streaming with sentence-level TTS chunking, no paid audio APIs.
