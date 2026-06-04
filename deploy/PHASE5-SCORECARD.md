# Phase 5 — call scorecard log

Per-call record of Aria naturalness iteration. Objective metrics from console-log
`[metric]` lines; subjective from the operator's ear (the 7-point scorecard in
`PHASE5-NATURALNESS-PLAN.md`). Latency source of truth is
`prototypes/latency-explainer.html` (snapshots seeded from these same numbers).

Legend: TTFA = time from end-of-your-speech to first audio (includes the VAD silence
tail). TTFT = `claude_ttft_ms`, pure Claude latency (request sent → first text token,
round 0 = the perceived lead-in). LLM = time to first streamed *sentence* (TTFT + the
chunker's sentence-buffering). TTS = synth time for the first sentence. All ms.

TTFT vs LLM is the key split: TTFT isolates what Claude itself costs; LLM−TTFT is what
sentence-chunking adds; TTFA−LLM is the VAD tail + TTS. Read `[brain]` log lines for the
full streamed text per round (incl. lead-in before a tool call) and per-round TTFT.

---

## Phase 6 — OpenAI Realtime backend (A/B vs local) — 2026-06-02

`REALTIME_BACKEND=openai`, `gpt-realtime`, voice `marin`. See `PHASE6-REALTIME-SPIKE-PRD.md`.

| metric | local (Phase 5) | realtime (Phase 6) |
|---|---|---|
| first_audio (model portion) | ~1000ms | **~333ms** (live, 319–498) |
| perceived TTFA | ~1.3s typical | ~0.5–0.9s observed |
| tools | lookup/slots/book/reschedule/log_call | **same** (function-calling, 0 errors) |
| barge-in | supersede/coalesce | server-VAD + response.cancel (clean) |
| voice | Kokoro `af_heart` | OpenAI `marin` |
| STT misses ("Settl"→"shuttle") | yes | yes (fused STT; same word issue) |

**Calls:** `phase6a` (Gate 1, first_audio 319–498ms) · `phase6b` (input-transcription +
barge-in guard) · `phase6c` (tools: live lookup→reschedule→log_call, 0 errors; tools showed
2× in trace = display bug, fixed) · **`phase6d` (marin) — confirmation ✅**: tools log once,
first_audio 387–450ms (one 2.2s log_call/goodbye outlier), 0 errors, marin voice.

**Phase 6 = feature-complete.** Realtime backend: ~333–450ms, full tool parity, clean
barge-in/trace, marin voice. Decision (keep / hybrid) pending the Phase 7 comparison.

**Live call on `gpu-phase7b` (2026-06-02, post-SLM-probe redeploy):** 5 turns, **0 errors**.
first_audio **424 / 347 / 371 / 365 / 337 ms** (median ~365). `lookup_lead` (Alex/Acme Movers,
demo_booked) + `log_call` (289ms, accurate summary) clean; pricing answered with **no
unnecessary re-lookup**; clean close. Confirms the realtime path is unaffected by the phase7b
overlay (SLM resident in-process doesn't touch realtime calls).

**Ground-truth audio measurement (2026-06-03, `scripts/call_timing.py`):** the numbers above
are app-trace `speech_stopped → first_audio` — the **model slice only**. An independent oracle
that splits the Telnyx dual-channel mp3 and energy-VADs each track measures the **full perceived
gap** the caller actually hears (acoustic end-of-caller-word → acoustic start-of-Aria):

| call | path | median gap | p95 | spread |
|---|---|---|---|---|
| `497e7604` (marin) | realtime | **1430ms** | 1593ms | tight |
| `6cd64767` (earlier) | local pipeline¹ | **1980ms** | 2810ms | fat tail |

¹ backend inferred from timing (pre-redeploy); its `/debug/turns` died with the pod, unconfirmed.

So **perceived realtime latency is ~1.4s, not ~0.33s** — the ~1.1s delta is everything the app
metric excludes: server-VAD endpointing silence (~500ms) + Telnyx playout/jitter buffer + network
(500 + 333 + ~600 ≈ 1.43s reconciles). The oracle independently reproduces the project thesis from
audio alone: **realtime beats local by ~550ms at median AND has a far tighter tail** (1593 vs 2810
p95), matching Claude's documented TTFT variance. The "~0.5–0.9s observed" row above was app-derived
and optimistic; treat **~1.4s as the honest perceived number**. Caveat: absolute ms carry a small
energy-VAD offset bias (trailing fricatives can clip caller-end early, inflating gaps slightly);
the *relative* realtime-vs-local comparison is robust. This is why perceived TTFA is now measured
from audio, not just the trace.

**Phase 7 LIVE cascade — first real SLM-in-pipeline calls (2026-06-04, `gpu-phase7c`).**
`BRAIN_BACKEND=slm` (Qwen3-4B-Instruct-2507 bf16) + `STT_STREAMING=1` + KV-prewarm, on the local
pipeline (`REALTIME_BACKEND=local`). Two calls: one messy/interrupted, one clean. In-app trace +
the audio oracle.

What works: SLM tool-calling correct (lookup_lead/log_call, 0 errors), **SLM TTFT ~420–510ms**
(matches the probe), and **streaming STT fully fires — `stt=0ms` on every turn of the clean call**
(transcript ready at end-of-speech; lever #1 delivered). Trace split: **chat first-sentence median
~520–656ms; tool turns ~2.4–2.6s** (the two-pass penalty, micro-ack-masked).

Oracle methodology upgrade (prompted by an operator-perception mismatch — "didn't feel 2s"): added
**Silero VAD** segmentation (`--vad silero`, the Daily.co benchmark standard, ~30ms) alongside the
energy method. **Silero CONFIRMED energy — it was NOT over-measuring** (clean call 2000ms silero vs
2060ms energy). Re-measured both calls with Silero, apples-to-apples:

| call | path | oracle median (Silero) | note |
|---|---|---|---|
| `497e7604` | realtime | **1250ms** | corrects the 1430ms energy figure above |
| `529f5a6a` | cascade (clean) | **2000ms** | time-to-substantive-content |

**Dual metric (time-to-first-sound vs time-to-content) — and a RETRACTION.** The oracle now
reports both (an operator said the cascade "didn't feel 2s"; we suspected micro-acks were masking).
The data **refuted that**: on the clean cascade call most turns had NO ack (first-sound = content),
and the one ack that fired landed at ~2.2s, not ~1.4s. So:

| call | first-sound (median) | content (median) |
|---|---|---|
| `497e7604` realtime | 1250ms | 1250ms |
| `529f5a6a` cascade | **2000ms** | **2000ms** |

There is **no ack-masking story** — the cascade's first-sound is ~2000ms too. An earlier scorecard
draft claimed the ack masked the gap to ~1.4s; that was wrong and is retracted.

**What's robust vs uncertain.** ROBUST: the *relative* result — cascade is **~750ms slower than
realtime**, across two VAD methods (energy+Silero), both metrics, AND the in-app trace (ttfa 1168 vs
333). UNCERTAIN: the *absolute* perceived ms — the gap is measured at the Telnyx recording boundary,
which can carry a constant inter-leg channel-skew; the operator's ear (felt < 2s) is a valid signal
that absolute perceived is lower. Settling the absolute needs a clock-synced measurement at the
caller's device (a PSTN call doesn't give us that) — logged as an open item, not asserted.

**Verdict (holds on the robust axis):** realtime wins time-to-answer by ~750ms and on the tail
(cascade tool turns ~2.4–3.5s to content). The cascade's case is cost/control/self-hosting, **not**
latency. Next lever if revived: kill the tool-turn two-pass tail (speculative/parallel tool exec).
Phase 7 stays PARKED; realtime remains the production path.

**Phase 7 (candidate) — SLM+Claude cascade:** research + **probe measured** (2026-06-02,
image `gpu-phase7b`, `Qwen/Qwen3-4B-Instruct-2507`, `scripts/slm_probe.py` via `/debug/slm_probe`,
on-GPU with whisper+kokoro resident). Native chat template + our REAL tool schemas + persona.

| metric | bf16 raw prompt | bf16 **tuned** (`gpu-phase7b`) | 4-bit (nf4) |
|---|---|---|---|
| TTFT first-token (median / p95) | 369 / 377 ms | **376 / 380 ms** | 468 / 480 ms |
| tool-calling (our 6-turn corpus) | 4/6 | **5/6** | 3/6 |
| VRAM after load (alongside whisper+kokoro) | 10027 MB / 20470 | 10027 MB / 20470 | 10775† |

†4-bit VRAM contaminated — the endpoint doesn't free the prior bf16 model in-process
(probe limitation; bf16's number is the clean one). before-load baseline = 2453 MB
(whisper `medium.en` + kokoro). GPU util ~77–78% during a single bf16 generation.

**Findings (reason, don't assert):**
- **bf16 beats 4-bit on BOTH latency and tools** — nf4 dequant adds ~100ms TTFT and cost a
  tool case. VRAM headroom is ample (10 GB free at bf16), so 4-bit's footprint win is pointless.
  → **run bf16.** This overturns the research note's "Q4 / 3.4 GB" assumption.
- **TTFT win is real:** ~376 ms vs Claude's ~600 ms network TTFT. Cascade TTFA ≈ 376 + ~130
  STT + ~160 chunk + ~124 TTS ≈ **~790 ms — under 1s, self-hosted.** Still > realtime's ~333 ms
  first-audio — **realtime is unbeaten on raw speed; the cascade is close, and it's ours.**
- **BUT ~790 ms is best-case (chat / non-tool turn only).** A tool turn can't stream the tool
  call into speech — it must fully generate the `<tool_call>` block, run the tool (~170–370 ms:
  lookup 366 / slots 169 / log_call 289), then a SECOND SLM pass produces the spoken answer.
  So a single-tool turn is ≈ **1.3–1.6 s+ ex-VAD**, and multi-tool turns stack worse — exactly
  where a sales call lives. The micro-ack masks *perceived* latency, not actual. This is the
  cascade's real cost vs realtime (which also pays tool latency, but with a faster fused model).
- **Prompt fixes worked → 4/6 → 5/6** (TTFT unchanged, so the directive is free):
  - ✅ `lookup_lead` fixed — strip the persona's "lead-in before you act" rule on the SLM path +
    "emit tool calls immediately, no preamble" (caller silence is covered by the micro-ack).
  - ✅ `reschedule` fixed — sharpened book/reschedule descriptions to disambiguate new-vs-move.
  - ❌ new miss `find_slots` → called `lookup_lead` (re-identify before booking). Largely a
    **harness artifact**: every case reuses the same forceful inbound "look them up right away"
    context; mid-call that wouldn't repeat. In a real call it's one extra cache-hit lookup round,
    then it offers slots. Both negative cases (pricing, backchannel) still correctly stay chat.
  - Fixes are SLM-path-only (in `slm_probe.py`, not persona.py/tools.py) — Claude+realtime keep
    the lead-in (good there for masking tool latency). `tuned=0` runs the verbatim persona for A/B.
- **VRAM coexistence: a non-issue.** Compute contention under *simultaneous* whisper+kokoro
  inference is the only thing left to watch (per-stage trace + `/debug/gpu` will catch it).

**Cascade shape (confirmed):** SLM (Qwen, bf16, Kokoro voice) is the DEFAULT brain; **Claude is
the escalation/fallback** for ambiguous / multi-tool turns. NOT realtime+SLM — the cascade is a
self-hosted *alternative* to realtime. **Not wiring it up now** (operator call, 2026-06-02): the
probe already gave the go/no-go on the SLM brain, and the tool-turn latency above shows the
cascade loses its edge precisely on tool-heavy turns — so the latency case for switching off
realtime is weak. Realtime stays LIVE. Phase 7 is **parked** with a clear picture: revisit if
vendor lock / per-min cost / voice-control become the priority over raw latency. If revived:
wire Qwen into the local pipeline + heuristic router, and re-probe with a bigger tool corpus
(5/6 on 6 cases isn't a trustworthy %).

---

## ✅ Phase 5 CLOSED — summary (2026-06-02)

**Latency:** 1.88s (Pass 1.1) → **~1.3s typical / ~1.05s floor** TTFA. STT (~130ms) and TTS
(~124ms) hit the operator targets; the residual gap is Claude TTFT (~600ms + 2–4s spikes),
now **masked** by the latency-triggered micro-ack. No in-architecture lever remains.

**Realtime baseline (probe, not a call):** `scripts/realtime_probe.py` on `gpt-realtime`,
text-in, manual turn detection → **first_audio median 312ms** vs our ~1000ms model portion
(**~688ms faster**). → `PHASE6-REALTIME-SPIKE-PRD.md`.

**Validated on real calls:** micro-ack fires only on spikes (stayed silent on healthy calls);
`log_call` dedupe; `interrupted` partial-transcript; backchannel immunity; no audio starvation
(`during_think=0` throughout). Recurring STT miss: **"Settl"→"shuttle"** (fix available via
whisper vocab hint).

---

## Call 6 — micro-ack live, no spikes to mask (image `gpu-phase5l`, VAD 300ms) — 2026-06-02

Healthy call: every TTFT 550–832ms (no spikes), so the micro-ack correctly **never fired** —
validating it as spike-insurance, not an every-turn ack. TTFA 1.05–1.58s.

| # | Caller said | TTFT | TTFA | note |
|---|---|---|---|---|
| 1 | "schedule my demo" (barged greeting) | 798 (r1 947) | 1552 | greeting barge-in handled |
| 2 | "Yes." | 553 | 1054 | |
| 4 | "...how much shuttle is" | 552 | 1431 | "Settl"→"shuttle" STT miss; Aria recovered |
| 6 | "that'll be all" | 739 | 1247 | `log_call` once |
| 7–10 | "Bye" ×4 | 550–712 | 1.0–1.4s | closing loop: 6 supersedes, `during_think=0` |

`pre-rendered 7 micro-acks` ✓ · `log_call` dedupe ✓ · `interrupted` flag ✓. Parked: the
multi-"Bye" closing loop (idle backchannels become turns — deliberately not suppressed, since
a lone idle "yeah" can be a real answer).

---

## Call 5 — observability live + best latency yet (image `gpu-phase5i`, VAD 300ms) — 2026-06-01

First call with structured `/debug/turns` + `/debug/errors`. Caller barged the greeting,
asked to schedule, then interrupted at the end and hung up.

| # | Caller said | TTFT | TTFA | post-VAD | LLM 1st | TTS | opener | notes |
|---|---|---|---|---|---|---|---|---|
| 1 | "I'd like to schedule my demo" | 624 (r1 945) | **1335** | 1035 | 781 | 124 | "Perfect!" | barged the greeting first (real 1888ms utt → superseded ✓); lookup 366ms |
| 2 | "schedule another demo" | 522 | **1288** | 988 | 712 | 146 | "Got it —" | find_open_slots 169ms; interrupted ("What is?" 864ms) → hung up; `aria_text=null` |

**Best latency yet — TTFA 1335 / 1288ms**, both under 1.4s. VAD 300 + chunker + short
openers compounding.

**Interruptions both handled correctly:**
- Greeting barge-in: a real 1888ms utterance superseded the greeting (not a backchannel) —
  `supersedes=1 over_audio=1`, exactly right.
- End interruption: "What is?" (864ms) arrived during round-1 *think* → `interruption during
  think — not cancelling, follow-up queued` (`during_think=0`), then caller hung up before
  the queued reply played. That's why turn 2's `aria_text=null`.

**Observability validated:** `/debug/turns` returned the full per-turn trace (caller/Aria
text + timestamps, additive `breakdown_ms` summing to TTFA, per-round LLM in/out, tool
timing). `/debug/errors` empty. The `breakdown_ms` for turn 1: vad 300 + stt 130 + ttft 624
+ chunk_buffer 157 + tts 124 = 1335 = TTFA, exactly.

**Opener finding (closes the chunker thread):** Claude prefers `"Got it —"` (em-dash),
`"Perfect!"`, `"Sure thing."` over a bare `"Got it."` So the period-guard fix (8→7) rarely
*fires*, but `!` openers already flush and dash openers' following clause is short
(chunk_buffer ~190ms). **Openers are no longer a latency bottleneck — no further chunker
work needed.**

**Follow-on deploys (same session):**
- `gpu-phase5j` — partial transcript on interrupted turns (the turn-2 `aria_text=null` case
  now captures what Aria got out + `interrupted=true`, and writes it to session history so
  Claude won't repeat). DEPLOYED.
- `gpu-phase5k` (next) — log_call dedupe (fires once/call) + persona "log once, don't
  re-pitch on closing pleasantries." Fixes the Call-4 double/triple `log_call`. NOT
  attempting blanket idle-backchannel suppression: a lone "yeah"/"okay" while idle can be
  a real answer to Aria's question (e.g. "does Tuesday work?" → "yeah"), so suppressing it
  would drop bookings. Dedupe + persona is the safe fix.

---

## Call 4 — chunker `.`-guard 8→7 (image `gpu-phase5h`, VAD 300ms) — 2026-06-01

Cooperative call testing the "Got it." chunker fix. (demo time → pricing → thanks.)

| # | Caller said | TTFT | TTFA | post-VAD | LLM 1st | TTS | opener | notes |
|---|---|---|---|---|---|---|---|---|
| 1 | "what time is my demo" | 673 (r1 624) | 1468 | 1168 | 867 | 156 | "Let me pull up your info…" | lookup; lead-in masked it ✓ |
| 2 | "how much will it cost" | 728 | **1595** | 1295 | **973** | 113 | "Sure thing." | pricing — see below |
| 3 | "that'll be all, thanks" | 1142 | 1739 | 1439 | 1143 | 125 | "You're welcome!" | TTFT spike (network) |

**Result vs Call 3 (the win):** the **pricing turn TTFA dropped 2137 → 1595 (−542ms)** and
LLM-1st-sentence 1481 → 973 (−508ms). All turns now land in a tight **1.47–1.74s** band
(was 1.1–2.4s). `supersedes=0 during_think=0 over_audio=0` — clean.

**Caveat — fix not directly exercised this call.** Claude opened pricing with "Sure thing."
(10 chars, flushes under *both* old guard 8 and new guard 7), not a literal 7-char "Got
it." So the −542ms is real but partly opener-luck; the 7-char path is proven by the
offline chunker sim + zero regression here. Watch for a "Got it." instance next call.

**New minor issue (parked, end-of-call polish):** an idle "Thank you." (0.7s) became a
full turn and fired `log_call` a 2nd/3rd time. The backchannel filter only runs while
Aria *holds the floor*; utterances arriving while she's idle always become turns. Claude
also re-logs on every closing pleasantry. Harmless (same CRM row) but wasteful — candidate
fixes: "log_call once" persona rule (like the lookup_lead guard) + treat a lone
backchannel-word idle utterance as non-turn.

**Caching:** still `cache_read=0 / cache_write=0` — confirmed inert at this prompt size.

---

## Call 3 — TTFT + caching instrumentation (image `gpu-phase5g`, VAD 300ms) — 2026-06-01

First call with `claude_ttft_ms` + per-round `[brain]` stream logging + prompt-cache
attempt. Cooperative call (appointment check → "what does Settl do" → pricing ×2 → thanks).

| # | Caller said | TTFT | TTFA | post-VAD | LLM 1st | TTS | notes |
|---|---|---|---|---|---|---|---|
| 1 | "check my appointment today" | 688 (r1 1172) | 1487 | 1187 | 900 | 77 | lookup; lead-in "Let me pull up your info" masked it ✓ |
| 2 | "what does Settl do" | 531 | 1319 | 1019 | 742 | 141 | clean one-liner + question |
| 3 | "Um..." | 667 | 1796 | 1496 | 881 | 67 | caller superseded (over-audio 3680ms) → re-asked |
| 4 | "how much" | **702** | **2137** | 1837 | **1481** | 170 | ⚠️ "Got it." trap; superseded (1792ms) |
| 5 | "how much" (repeat) | 1071 | 2431 | 2131 | 1827 | 144 | full founding-rate pitch delivered |
| 6 | "understood, thank you" | 595 | 1111 | 811 | 595 | 71 | first token *was* the sentence; log_call fired |
| **avg** | | **709** | **1714** | **1413** | **1071** | **112** | |

**Key findings:**
- **TTFT instrumentation works.** Pure Claude latency is **~530–700ms** (spike 1071),
  the cleanest floor reading we have. The split is now legible: TTFA−LLM = VAD tail + TTS;
  LLM−TTFT = chunker buffering; TTFT = Claude itself.
- **Prompt caching is INERT** — `cache_read=0` AND `cache_write=0` on every turn. The
  tools+system prefix is **under Haiku's 2048-token floor**, so Anthropic never created a
  cache entry. Even if padded over the line, prefill of a <2048-tok prompt is already fast
  — **our TTFT is network+inference bound, not prefill bound.** Decision: keep the code
  (correct; free cache-stat logging; auto-engages if the knowledge blob ever grows past
  2048) but do NOT artificially pad the prompt. Dead end at this prompt size.
- **The chunker "Got it." trap, now quantified.** Turn 4: TTFT 702 but first-sentence
  1481 = **779ms lost.** Claude opens "Got it." (7 chars); the `_MIN_CHARS=8` guard on `.`
  refuses to flush it, so TTS waits for the whole next clause. Fix = lower the guard to 7
  (lets "Got it." / "Got you." flush; still protects "Mr." / "9 a.m."). ~−780ms TTFA on
  every acknowledgement-opener turn. **This is the next change** (keep the fillers — fix
  the chunker, don't strip the openers).

**Orchestration health (phase5e starvation fix holding):**
- `call-summary: supersedes=2 during_think=0 over_audio=2` — **zero silent/zero-audio
  cancels.** The phase5e audio-starvation regression is gone.
- Backchannels "Yep." (640ms) and "Thank you." (704ms) correctly **ignored**.
- `log_call` + CRM row update + recording-url write all fired at hangup ✓.

**7-point read (operator):** flow good, lookup masked by lead-in, pricing landed; the two
pricing re-asks were caller talk-over (supersede worked, not a bug). Pauses still the
chunker trap above. To verify next pass: chunker fix drops the pricing-turn TTFA toward ~1.4s.

---

## Call 2 — Pass 1.1 (image `gpu-phase5b`, VAD 300ms) — 2026-05-31

Adversarial test: misheard opener, rapid pricing questions, lots of talking over Aria.

| # | Caller said | TTFA | post-VAD | LLM 1st | TTS | source | notes |
|---|---|---|---|---|---|---|---|
| 1 | "...cook my fas kabob" (STT miss) | 1999 | 1699 | 1492 | 60 | answer | barge-in after |
| 2 | "when my demo is" | 1337 | 1037 | 811 | 49 | answer | lookup (fresh); barge-in after |
| 3 | "how much does Settl cost" | 2146 | 1846 | 1668 | 46 | answer | lookup **cache hit ✓**; barge-in after |
| 4 | "how much that'll cost" | 1972 | 1672 | 1488 | 34 | answer | lookup **cache hit ✓**; barge-in after |
| 5 | "don't pull up my info, how much" | 1944 | 1644 | 1416 | 45 | answer | full pricing answer delivered |
| **avg** | | **1880** | **1580** | **1375** | **47** | | |

**Subjective (operator):** "Way better — conversation felt more natural." Attributed to
the **VAD 300ms** change (snappier turn-taking), NOT TTFA. TTFA still ~1.9s, flagged for
Pass 2 (operator already had it on the Pass-2 list).

**7-point read (operator qualitative, not yet scored 1–5):**
1. Not interrupted — **weak.** 4 barge-ins; caller talked over Aria repeatedly.
2. Snappy — **mixed.** Turn-taking felt better (VAD), but TTFA ~1.9s still a real pause.
3. Human — good (varied openers, natural pricing pitch).
4. Tight — good (no feature-dump; pricing answer was concise + ended on a question).
5. Opener — n/a this call (caller jumped straight in).
6. Heard me right — turn 1 STT miss ("cook my fas kabob"); rest fine.
7. Overall — better feel, but the lookup loop + TTFA made the price take 5 turns to land.

**What this call surfaced (→ Pass 2/3):**
- **TTFA did not improve** (1670→1880 avg). The reaction fast-path never fired — Claude
  opens with "Got it —" / "Got it." (comma/dash), and the chunker only fast-flushes
  `!`/`?`. So first audio still waits the full first sentence. **The latency lever we
  shipped is inert in practice.**
- **Unnecessary `lookup_lead` on off-topic turns.** Caller asked price 3×; Aria re-ran
  lookup each time (cache hit, but still an extra LLM round before answering). Caller
  literally said "you don't need to pull up my info." → persona should not look up when
  the question doesn't need identity.
- **Barge-in spiral.** ~1.9s TTFA + caller filling the silence → barge-in cancels the
  forming turn → caller repeats. Backchannel immunity + endpointing (Pass 2 A) is the fix.
- **Cache works ✓** (turns 3, 4 logged "cache hit").

---

## Call 1 — Pass 1 baseline (image `gpu-phase5a`, VAD 400ms) — 2026-05-31

| # | Caller said | TTFA | post-VAD | LLM 1st | TTS | source |
|---|---|---|---|---|---|---|
| 1 | "when is my demo" | 1584 | 1184 | 991 | 54 | answer (lead-in masked lookup ✓) |
| 2 | "yeah, that works" | 1971 | 1571 | 1314 | 141 | answer (worst — naked LLM pause) |
| 3 | "no, thank you" | 1455 | 1055 | 868 | 53 | answer |
| **avg** | | **1670** | **1270** | **1058** | **83** | |

**Subjective (operator):** "Flow is better. Pauses still noticeable." Lead-in fired on the
tool turn; plain-turn pause (~2s) was the standout problem.

---

## Cross-call takeaway

- **Confirmed win:** VAD 400→300ms — felt-natural turn-taking.
- **Not a win:** TTFA / reaction-opener fast-path — inert because openers aren't `!`/`?`.
- **Dominant cost:** LLM first-sentence (0.8–1.7s), noisy. Real latency work is Pass 3.
- **New Pass-2 items:** barge-in immunity + endpointing; don't `lookup_lead` when the
  question doesn't need identity.
