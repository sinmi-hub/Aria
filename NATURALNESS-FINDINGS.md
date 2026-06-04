# Naturalness & turn-taking — PARKED findings

Aria's tools/booking work end-to-end (verified live 2026-05-31). What's parked is making
her *sound* natural and feel responsive. This is its own module — collected observations
from real calls so we don't re-discover them. Do NOT fix piecemeal; tackle as one pass.

## Observed on the 2026-05-31 booking call (logs)

1. **Repeated `lookup_lead` (4× in one call).** Barge-ins cancel the in-flight turn; the
   next turn re-streams from scratch and Claude re-decides to call `lookup_lead`, even
   though the lead is already in `AriaTools.self.lead`.
   - Persist tool results across barge-in-cancelled turns so Claude knows it
     already looked up.

2. **Replies are too long → they invite barge-in.** e.g. reading "9, 9:30, 10, 10:30,
   11, or 11:30" out loud. Long TTS spans give the caller room to talk over her, which
   cancels the turn mid-sentence. Tighter replies (offer 2 times, not 6) = fewer
   interruptions. Tune the system prompt for brevity + cap `max_tokens` lower.

3. **Latency: first-answer 1.5–2.5s typical, one spike to 5.3s.** A 0.6s utterance once
   took **3.3s to STT** — likely STT thread contention while tool calls / overlapping
   turns ran. Watch `asyncio.to_thread` contention between STT and tool dispatch.
   - With `FAST_ACKNOWLEDGEMENT` removed there's now no filler before her first word;
     consider a *tasteful, varied* filler ("let me check") only before slow tool turns,
     not every turn (the old every-turn "Got it" is what we removed).

4. **Barge-in very sensitive.** Many turns cancelled. Could be VAD threshold + the long
   replies combined. Revisit `vad_silence_threshold_ms` / barge-in gating together with
   reply length.

5. **"2 AM" handling.** STT heard the caller's "2 AM" correctly; Aria correctly flagged
   it wasn't available and offered mornings — but it took several back-and-forth turns.
   Acceptable, but tighter clarification would help.

## Copy / prompt issues (smaller, could fix sooner)
- Aria says **"you'll get a calendar invite shortly"** — not true until DWD is wired (see
  TODO.md). Either wire DWD or change the line to "it's on our calendar / I'll have the
  team confirm."
- **Greeting** (`AGENT_GREETING`) is generic ("Hi, this is Aria, how can I help?"). For a
  sales agent it should identify Settl and (for two-party-consent states) announce
  recording. Currently recording-consent is only reactive ("if asked, say yes").

## Where to start when unparked
`config.py` (greeting, max_tokens, VAD), `app/agent/persona.py` (brevity rules),
`app/agent/tools.py` (`_t_lookup_lead` cache), `app/agent/pipeline.py` (barge-in gating).

---

## Update after Pass 1 + 1.1 (2026-05-31) — see `deploy/PHASE5-SCORECARD.md`

**Done:** greeting now IDs Settl + announces recording; `max_tokens` 200→160; lookup
cache (`cache hit` confirmed live); "calendar invite" line removed; VAD 400→300
(**the confirmed win — operator: turn-taking feels natural**); persona brevity + reaction
opener + direction-aware verbosity.

**Still open / newly confirmed (Pass 2/3):**
1. **TTFA unchanged (avg 1670→1880ms).** The reaction-opener fast-path is **inert**: the
   chunker only fast-flushes `!`/`?`, but Claude opens with "Got it —" / "Got it."
   (comma/dash) → first audio still waits the whole first sentence. Fix options: flush a
   short leading clause before the first comma/dash too (watch abbreviations), a real
   pre-roll/contextual filler, or attack LLM first-sentence latency directly. LLM
   first-sentence (0.8–1.7s, noisy) is the dominant cost — this is the Pass-3 latency core.
2. **Don't `lookup_lead` when the question doesn't need identity.** Call 2: caller asked
   price 3× and Aria re-looked-up each time (cache hit, but still an extra LLM round before
   answering). Caller said "you don't need to pull up my info." Persona/tool-gating fix.
3. **Barge-in spiral confirmed.** ~1.9s TTFA + caller filling silence → barge-in cancels
   the forming turn → caller repeats. Backchannel immunity + adaptive endpointing (Pass 2
   workstream A) is the fix; lower TTFA reduces the trigger.

## Pass 2 diagnostic + design (in progress)

**Hypothesis (to test, not assume):** the spiral is an orchestration bug, not TTS/VAD.
`speaking` is set true at STT (`pipeline._handle_turn`) — before any audio — and stays true
through LLM + tool think-time, so `_barge_in` can fire during *silent* windows and cancels
the whole turn (tool results aren't persisted → Claude re-runs `lookup_lead`). Latency and
the spurious lookup are *triggers* (how much silence exists); the `speaking`-state + cancel-
everything semantics are the *amplifier* (whether filled silence detonates the turn).

**Instrumentation shipped (gpu-phase5c, behavior unchanged):** every barge-in now logs
`SILENT-WINDOW cancel` vs `over real audio` (+ `first_audio_sent`, ms into turn); call close
logs `[call-summary] barge_ins=/silent_window=/over_audio=`. Counts silent-window cancels vs
legitimate interrupts.

**Test plan:** one cooperative call (don't talk over) + one adversarial (talk over) → compare
`silent_window` counts. Then design the fix.

**Design bias (operator, for the fix):** production must weight *talk-over* heavily — real
callers interrupt. So the fix should make interruption **graceful** (supersede the stale
response: capture the new utterance + abort generation), NOT merely suppress it. `speaking`
should mean "audio is flowing," and sub-~300ms backchannels shouldn't fire at all.
