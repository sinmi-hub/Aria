# Phase 6 — OpenAI Realtime spike (PRD)

**Status:** ✅ DONE — **CHOSEN as the production path** (2026-06-02). Confirmation calls passed
(marin, first_audio ~333–365ms, 0 errors); the keep/hybrid decision resolved in realtime's favour
after the Phase 7 cascade probe (cascade parked — see `PHASE7-CASCADE-SLM-NOTES.md`). **Model:**
`gpt-realtime` (GA), voice `marin`. **Images:** `gpu-phase6a..d`, then `gpu-phase7b` (current
master; realtime + idle SLM-probe deps). No live pod right now (killed at session end).

**Progress (2026-06-02):** Gate 0 ✅ (μ-law passthrough), Gate 1 ✅ (live first_audio
~333ms median, ~3× faster), Gate 2 ✅ (all 7 tools via realtime function-calling, feature
parity — lookup/slots/book/reschedule/log_call, 0 errors). Barge-in clean. Voice auditioned
(`scripts/voice_audition.py`) → **marin**.

## 1. Why

Phase 5 floored the current pipeline at **~1.05s perceived TTFA** (typical ~1.3s). STT
(~130ms) and TTS (~124ms) already meet the operator targets; the whole remaining gap is
**Claude TTFT (~600ms, with 2–4s spikes)**. We instrumented, masked the spikes (micro-ack),
and confirmed there is no further in-architecture lever short of a closer/faster endpoint.

The measurement probe (`scripts/realtime_probe.py`, `gpt-realtime`, text-in, manual turn
detection) returned **first_audio median 312ms** (308–321, p95 321) vs our **~1000ms model
portion** (TTFA minus the 300ms VAD tail) — **~688ms faster, roughly halving perceived
latency.** A speech-to-speech model collapses STT→LLM→TTS into one hop, which is exactly the
slice we can't otherwise beat. This spike tests whether that win holds on real calls and is
worth the tradeoffs.

## 2. Goals / success criteria

- **Live A/B on real calls.** A realtime backend, flag-gated, measured by the *same* trace +
  scorecard as the local pipeline.
- **Sub-second perceived TTFA:** target **< 800ms** (speech_stopped → first audio), stretch
  **< 600ms**, measured **from the RunPod pod** (not just locally).
- **Preserve capability:** tool calls (lookup_lead / find_open_slots / book_meeting /
  reschedule / log_call / flag_for_human / mark_do_not_call), barge-in/interruption,
  Telnyx recording, CRM logging, and the recorded-call compliance line.
- **Zero risk to what works:** `REALTIME_BACKEND=local` (default) leaves the Phase 5 pipeline
  untouched and instantly revertible.
- **Output a decision:** keep realtime / revert / hybrid — backed by scorecard data.

## 3. Non-goals

- Not deleting the local pipeline — it's the fallback **and** the A/B baseline.
- Not reproducing Kokoro `af_heart` exactly — pick the closest OpenAI voice, operator signs off.
- Not touching the Telnyx telephony layer (webhook/answer/record stay as-is).
- Not building a general multi-provider abstraction beyond the single backend flag.

## 4. Architecture

```
Telnyx media WS  ──μ-law 8k──▶  realtime bridge  ──▶  OpenAI Realtime WS
     (caller)     ◀─μ-law 8k──   (new component)  ◀──   (gpt-realtime)
```

- **New `app/telephony/realtime_bridge.py`** — a `CallPipeline`-shaped class selected by the
  backend flag in `media_ws.py`, so the media WS recv loop is unchanged.
- **Audio format — g711 μ-law passthrough CONFIRMED (Gate 0 ✅ 2026-06-02).** OpenAI Realtime
  accepts `audio/pcmu` (g711 μ-law 8k) for both input and output with no latency penalty
  (probe: first_audio ~315ms on pcmu vs ~312ms on pcm). **Telnyx μ-law passes straight through
  with zero transcoding** — the bridge just shuttles base64 μ-law frames between the two
  WebSockets. The `app/audio/codec.py` transcode fallback is not needed.
- **Endpointing:** use realtime **server-VAD** (`server_vad` / `semantic_vad`) instead of our
  Silero tail; compare its delay to our 300ms.
- **Tools:** map `AriaTools.dispatch` to realtime **function definitions**; on `function_call`
  events run the tool in a thread, return output, let the model continue. Reuse `AriaTools`
  unchanged (it's transport-agnostic).
- **Barge-in:** realtime emits `input_audio_buffer.speech_started` → `response.cancel` +
  `conversation.item.truncate`. Map to our supersede semantics; re-validate the spiral stays dead.
- **Persona:** port `system_prompt()` + `SETTL_KNOWLEDGE` into the realtime session
  `instructions`. Keep the recorded-call line + DNC behavior.
- **Recording:** unchanged (Telnyx `record_start`).
- **Observability:** emit the **same `app/trace.py` events** from the realtime path so
  `/debug/turns`, `/debug/errors`, and the scorecard work identically. Stage breakdown differs
  (no separate STT/TTS) — record the realtime-native numbers: `speech_stopped → first_audio`,
  function-call round-trip ms, and any provider error events.

## 5. Work phases (each: build → rebuild image → live call → scorecard)

1. **PoC bridge** — Telnyx ↔ realtime, g711 passthrough (Gate 0), no tools. Just converse;
   measure live `first_audio` from the pod. **Gate 1.**
2. **Tools** — port function-calling for all seven tools; verify a lookup round-trip.
3. **Barge-in + endpointing** — server-VAD tuning; interruption parity; spiral re-check.
4. **Persona + compliance** — instructions, recorded-call line, DNC, voice selection.
5. **Observability parity** — trace/scorecard emit from the realtime path.
6. **A/B + decision** — alternate backends across calls; fill the scorecard; decide.

## 6. Instrumentation / success metric

Reuse `deploy/PHASE5-SCORECARD.md` format with a `backend` column (`local` | `openai`).
Headline metric: **speech_stopped → first_audio** (perceived TTFA). Target < 800ms from the pod.
Also track: function-call round-trip ms, interruption correctness, cost/min, voice acceptability.

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Voice loses Aria's warmth | Audition OpenAI voices (e.g. Marin/Cedar/alloy); operator sign-off before A/B |
| Cost ($/min audio in+out vs current tokens + free local STT/TTS) | Estimate per-call cost early; $5/day cap already in force; compare in the decision |
| Function-call round-trip adds latency | Measure per tool; lead-in instructions still apply ("say a line, then call") |
| Pod→OpenAI network differs from local probe | Re-measure from the pod in PoC (Gate 1), not just locally |
| Interruption semantics regress (spiral) | Reuse the Phase 5 turn test discipline; re-run adversarial calls |
| g711 μ-law unsupported | Fallback transcode path (Gate 0 decides) |
| Vendor lock-in | Local pipeline stays behind the flag, fully working |
| API key in transcript/.env | Rotate the experiment key after the spike; keep `deploy/.env` gitignored |

## 8. Decision gates / kill criteria

- **Gate 0 (PoC):** g711 μ-law passthrough works (else accept transcode cost).
- **Gate 1 (PoC): ✅ PASSED 2026-06-02.** Live `first_audio` 319–498ms (median ~333ms) through
  Telnyx from `gpu-phase6a` — ~3× faster than our ~1000ms model portion. The probe number held
  end-to-end. Greeting + conversation + persona confirmed on the call.
- **Gate 2 (tools): ✅ PASSED 2026-06-02.** All 7 tools fire via realtime function-calling
  (lookup/find_slots/book/reschedule/log_call), reusing `AriaTools.dispatch` — same logic as
  local. Live call: lookup → demo info → reschedule → log_call, 0 errors. (Tool-round latency
  to confirm on the marin confirmation call.)
- **Kill:** prohibitive cost/min, unacceptable voice, or interruption regressions that don't tune out.

## 9. Effort estimate

~**4–5 sessions** to a decision: PoC+Gate1 (~1), tools (~1), barge-in/persona/obs (~1–2), A/B (ongoing).

## 10. What carries over (already built)

Telnyx layer · recording · `AriaTools` (tools logic) · `app/trace.py` + scorecard · persona
content + `SETTL_KNOWLEDGE` · the latency targets · `scripts/realtime_probe.py` (the harness
that produced the 312ms baseline).
