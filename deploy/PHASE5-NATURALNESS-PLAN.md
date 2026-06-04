# Phase 5 — Naturalness overhaul (plan)

**Goal:** make Aria *feel* like a person on the phone — not cut off by "mhm," no dead
air, tight human phrasing, on-brand opener. Tool/booking logic is DONE and must not
regress.

**Ambition:** deep human-feel overhaul (all workstreams, real-call iteration).
**Done bar:** the operator's **call-scorecard** (below). We iterate until it's consistently good.

---

## ✅ Phase 5 — CLOSED (2026-06-02). Next: `PHASE6-REALTIME-SPIKE-PRD.md`

Latency journey: **1.88s → ~1.3s typical / ~1.05s floor** TTFA; spikes masked. Shipped on
`gpu-phase5l` (= `master`), all real-call validated:

- **A. Turn-taking & barge-in:** content-based backchannel policy (words/phrases, not
  duration), graceful supersede vs coalesce, audio-starvation fix (cancel only over real
  audio), partial-transcript capture on interrupt. Spiral dead.
- **B. Latency:** VAD 400→300; sentence-chunker `.`-guard 8→7 (pricing-turn −542ms);
  **latency-triggered micro-ack** (rotating, fires only on TTFT spikes — masks the 2–4s
  Anthropic-side spikes without the every-turn robotic feel). Prompt-cache attempt = inert
  (prefix under Haiku's 2048 floor; kept for the free cache-stat logging).
- **Observability:** `claude_ttft_ms` + per-round stream logging; structured per-turn trace
  (`app/trace.py` → `/debug/turns`, `/debug/errors`) with the additive latency breakdown;
  decode_ms/tok_s with an honest back-pressure caveat.
- **Response shaping / copy:** `log_call` fires once/call; "log once, don't re-pitch on
  goodbye" persona rule.
- **STT:** built our own faster-whisper grader (`scripts/stt_grader.py`) — replaces the
  audited jwulff/whisper-mcp (RCE confirmed). `Settl`→`shuttle` vocab fix available.

**Why Phase 6:** STT (~130ms) and TTS (~124ms) already meet targets; the whole remaining gap
is **Claude TTFT (~600ms + spikes)**, and there's no in-architecture lever left. The
`realtime_probe.py` harness measured OpenAI realtime at **first_audio ~312ms vs our ~1000ms
model portion (~688ms faster)** → Phase 6 spikes speech-to-speech behind a flag.

---

## The scorecard (rate every test call)
Score each 1–5 (5 = great); jot the single worst moment.

1. **Not interrupted** — Aria kept talking through "yeah/mhm/okay"; only stopped on a real interruption.
2. **Snappy** — replied fast, no awkward multi-second dead air.
3. **Human** — prosody + phrasing felt natural, not robotic or over-formal.
4. **Tight** — short replies; offered a couple options, didn't read long lists.
5. **Opener** — identified Settl, announced recording, sounded natural (not canned).
6. **Heard me right** — names/times understood or cleanly confirmed.
7. **Overall** — would a real prospect have found this acceptable?

Plus per call (objective): # unwanted barge-ins, # redundant tool calls, avg reply
length (from pod logs), and the latency stack (below).

**Latency source of truth: `prototypes/latency-explainer.html`.** This existing tracker
already models the turn stack (VAD silence → STT → LLM first sentence → TTS first audio →
TTFA) and has a "Make the Conversation Feel Natural" section with our exact levers. Its
fields map 1:1 to what the pipeline emits at `/debug/metrics`
(`ttfa_first_audio_ms`, `ttfa_first_answer_ms`, `llm_first_sentence_ms`,
`tts_first_answer_ms`, `vad_silence_threshold_ms`). **Each iteration: pull a real
`/debug/metrics` snapshot after a test call and drop it into the explainer's "Add a Real
Snapshot" / "Progress & Metrics" section** so we track movement across passes there — do
not build parallel instrumentation.

---

## Workstreams

### A. Turn-taking & barge-in  (highest felt impact, needs iteration)
Three distinct mechanisms — different feasibility, do not conflate:
- **(a) Backchannel immunity:** today barge-in fires on the VAD `speech_start` event the
  instant the caller makes any sound, cutting Aria off on "yeah/mhm." Change the
  barge-in trigger to require a *sustained* interruption (min duration / real utterance),
  tuned on real calls. Files: `app/agent/pipeline.py` (`_barge_in`/`feed_inbound`),
  `config.py` (VAD knobs), maybe `app/audio/vad.py`.
- **(b) Adaptive caller endpointing:** the real risk with talkative prospects — knowing a
  *long* reply is finished vs. just mid-thought pause. Too short → Aria jumps in mid-
  sentence; too long → dead air. Lever: an adaptive silence window — longer after the
  caller has been speaking a while, shorter after a brief reply. Tuned on real calls.
  Files: `config.py` (VAD silence knobs), `app/agent/pipeline.py`.
- **(c) Aria self-interrupt / turn cap:** Aria must not monologue. Cap her spoken turn
  (stop TTS after N sentences and yield), reinforced by the short-reply persona (C).
  Files: `app/agent/pipeline.py`, `app/agent/persona.py`.
- **No redundant tool calls:** cache the looked-up lead in `AriaTools` and short-circuit
  `_t_lookup_lead` when the phone already matches, so a cancelled+restarted turn doesn't
  re-hit Sheets (we saw `lookup_lead` fire 4× in one call). File: `app/agent/tools.py`.

### B. Latency & perceived responsiveness  (high impact)
- **Claude-generated contextual lead-in (the highest felt-impact change — Pass 1):**
  NOT a canned filler list, NOT a second model call. Claude opens *every* reply with a
  short contextual lead-in sentence ("good question, let me look") in its own words, off
  the transcribed turn. Because TTS is sentence-chunked, that first sentence reaches Kokoro
  within first-sentence latency while the rest of the reply (and any tool call) is still
  generating — so it costs ~nothing and feels like a person thinking out loud.
  **Critical sub-case — tool-first turns:** when the turn needs a tool call first
  (`find_open_slots`, etc.), Claude's output is a `tool_use` block with no leading text →
  nothing to speak during the ~1–2s tool latency. Fix: prompt Claude to emit a one-line
  lead-in *before* the tool call ("let me pull up the calendar"). Claude interleaves
  text + tool_use in one stream, so that lead-in plays over the gap. This is a persona +
  streaming change, no extra latency. Files: `app/agent/persona.py`, `app/agent/pipeline.py`.
- **Profile the STT spike:** a 0.6s utterance once took 3.3s to transcribe → thread
  contention (STT + Google tool calls + TTS all share the default `asyncio.to_thread`
  executor). Give STT its own executor or bump pool size. Files: `app/agent/pipeline.py`,
  maybe `app/runtime.py`.

### C. Response shaping  (quick win)
- Tighten persona to "one short sentence + a question"; offer 2 options not 6; lower
  `max_tokens` (200 → ~120). Shorter turns also reduce barge-in surface. Files:
  `app/agent/persona.py`, `config.py`.

### D. Copy & compliance polish  (quick win)
- Inbound **greeting**: identify Settl + announce recording up front (currently generic +
  only reactive — a two-party-consent gap). Files: `config.py` (`AGENT_GREETING`),
  `app/agent/persona.py`.
- Stop promising "you'll get a calendar invite shortly" (untrue until DWD).
- Always speak times naturally ("Tuesday at 2"), never raw ISO (persona already says this —
  reinforce).

### E. STT robustness  (lower priority — largely mitigated)
- Names de-risked by phone lookup already; add light time/number confirmation patterns in
  the persona. File: `app/agent/persona.py`.

### F. Speculative / predictive response  (STRETCH — experimental, flagged off, Pass 3 only)
The "model predicts where the caller is going and starts responding before they finish"
idea. **Honest feasibility:** real research technique (predictive turn-taking / full-
duplex), but HARD and RISKY on our turn-based STT→LLM→TTS pipeline. Cheap version = start
generating on a *partial* transcript and discard if the caller keeps talking — but if the
discard timing is even slightly off, Aria **blurts over the caller**, which scores worse on
"Not interrupted" than the dead air we're killing. True full-duplex is a different
architecture than we have.
- **Decision: do NOT commit this.** The 90% win is A(b) adaptive endpointing + A(c) short
  turns. Only attempt F if endpointing alone leaves the turn-taking score short, and even
  then behind a flag, off by default, scored explicitly against the blurt risk.

---

## Sequence (each pass = build → rebuild image → operator calls → scorecard → tune)
- **Pass 1 (quick wins + the lead-in):** C + D + the **Claude contextual lead-in incl.
  lead-in-before-tool-calls (B)** + the lookup-cache from A. All prompt/config/streaming-
  centric, low risk, one rebuild. The lead-in is the highest felt-impact item and it's a
  persona change, so it rides in Pass 1. Should jump the scorecard immediately (tighter,
  on-brand, no redundant lookups, no dead air before tool calls).
- **Pass 2 (turn-taking):** A(a) backchannel immunity + A(b) adaptive caller endpointing +
  A(c) self-interrupt/turn cap — the loop-heavy, real-call-tuned part.
- **Pass 3 (latency + stretch):** STT-executor fix (B). Only if needed: F speculative
  response (flagged, experimental).
- Re-score after each; stop when consistently good per the scorecard.

## Instrumentation
- **Latency:** already covered by `/debug/metrics` → fed into `prototypes/latency-explainer.html`
  (the source of truth). Don't duplicate it.
- **Add only what's missing:** an end-of-call summary log line for barge-in count,
  tool-call count, and avg reply length (the explainer covers latency; these three are the
  turn-taking/shaping signals it doesn't). Keep it to one `debug()` line on call close.

## Guardrails
- Do NOT change the brain's tool set, the Google adapters, or the deploy path.
- Keep every file < 300 lines. No new deps. Telnyx/Kokoro/Whisper stack unchanged.
- Voice stays Kokoro `af_heart` unless the scorecard says otherwise.
