# Phase 7 — cascade implementation (SLM-in-pipeline)

**Goal:** find the latency floor of the self-hosted cascade (VAD → STT → local SLM → chunker
→ Kokoro TTS) — "how far can we go" vs OpenAI Realtime's ~1.4s *perceived* gap (the
ground-truth number from `scripts/call_timing.py`, 2026-06-03).

**Scope decision (operator, 2026-06-03):** build everything **offline**, bring the pod up
**once** at the end to measure. **SLM-only** first — no Claude escalation, no router — so the
first number is the *pure* SLM floor, not confounded by escalation double-pay.

**The two levers** (from `PHASE7-CASCADE-OPTIMIZATION-RESEARCH.md`, ranked #1/#2):
1. **Stream STT→LLM** — today `_handle_turn` runs STT on the *whole* utterance *after*
   end-of-speech (sequential ~130ms). Overlap it: transcribe incrementally while the caller
   talks so the transcript is ready at the endpoint.
2. **Pre-warm the SLM KV cache** — pre-encode the static system+tools prefix once per call;
   reuse `past_key_values` so per-turn prefill is only the new tokens.

> Prerequisite the prior session's notes understated: **the SLM was never in the call path**
> (probe-only). Both levers require wiring Qwen in as the live brain first. That's P1.

## Phases

| P | What | Files | Needs GPU? | Status |
|---|------|-------|-----------|--------|
| 1 | Wire Qwen as a selectable brain (`BRAIN_BACKEND=slm`); tool-loop; mirrors `brain.stream_reply` contract | `app/agent/slm_common.py` (extracted shared), `app/agent/slm_brain.py`, `brain.py` dispatch, `runtime.py` load, `config.py` | load+generate yes; logic no | building |
| 2 | KV-prewarm: cache the system+tools prefix `past_key_values`, reuse per turn (`SLM_KV_PREWARM`) | `slm_brain.py` | measure yes | building |
| 3 | Streaming STT→LLM: overlap STT with speech (`STT_STREAMING`); transcript ready at endpoint | `app/audio/streaming_stt.py`, `pipeline.py` | measure yes | building |
| 4 | Measure on the pod: `call_timing.py` oracle + `/debug/turns`; decompose the gap | — | yes | pending pod |

## Design notes

- **Shared helpers** live in `app/agent/slm_common.py` (parse, prompt build, tool specs,
  SLM-path prompt fixes) so the probe and the live brain don't duplicate — `slm_probe.py`
  imports them. Pure-Python, unit-tested offline in `scripts/test_slm_brain.py`.
- **Tool turns** start with `<tool_call>` as the *entire* turn (the probe's `TOOL_DIRECTIVE`),
  so the brain buffers the first tokens, classifies tool-vs-speech, and never speaks tool JSON.
  Two-pass: pass 1 emits the call → dispatch via `AriaTools` → pass 2 speaks the answer.
- **KV-prewarm** is opt-in with a clean fallback to full prefill (correctness > speed) because
  it's `transformers`-version-sensitive and can't be validated off-GPU.
- **Streaming STT** is opt-in; it must NOT disturb the VAD/barge-in state machine (the
  documented "spiral"). v1 only overlaps STT timing — it does not change turn boundaries.

## Validation gates (on the pod)
- P1: a live SLM call answers + fires a tool correctly (parity with the probe's 5/6).
- P2: per-turn prefill tokens drop to ~the new-token count (trace `cache`/prefill ms).
- P3: post-VAD STT cost ≈ 0 (transcript ready at endpoint); perceived gap via the oracle.
- Headline: cascade perceived gap (oracle) vs realtime ~1.4s, decomposed
  [endpointing | STT-residual | SLM TTFT | chunk | TTS | playout].
