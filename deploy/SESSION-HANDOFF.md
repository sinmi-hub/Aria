# Voice Agent — Session Handoff (Phase 1 → Phase 2)

> **⚠️ CURRENT FRONTIER (2026-06-02): Phase 6 DONE (chosen); Phase 7 PARKED.** This doc is
> historical (Phase 1→2). Where things stand now:
> - **Chosen production path = OpenAI Realtime** (`gpt-realtime`, voice `marin`): first_audio
>   ~333–365ms, full tool parity, 0 errors on live calls. Image `gpu-phase7b` (= `master`).
> - **Phase 7 (SLM+Claude cascade) = PARKED.** Probed `Qwen3-4B-Instruct-2507`: ~376ms TTFT,
>   5/6 tools, but tool/escalation turns lose to realtime (and to Claude-alone). It's the
>   self-hosted alternative; revisit if vendor lock / cost / voice-control beats latency.
> - **No live pod right now** — the realtime line is OFFLINE (pod killed at session end; see
>   `RUNPOD-LIVE.md` for the one-command bring-up).
> Read in order: `PHASE5-SCORECARD.md` (full latency journey incl. Phase 6 live calls + the
> Phase 7 probe + decision), `PHASE7-CASCADE-SLM-NOTES.md`, `PHASE6-REALTIME-SPIKE-PRD.md`.

**Author:** Phase-1 build session, 2026-05-30.
**Audience:** the next (hands-off) agent or engineer who picks up Phase 2 (hosting).
**TL;DR:** A fully working open-source voice AI phone agent ("Aria") was built and
**proven on a real phone call** on CPU-only hardware. The only paid services are
Telnyx + Claude. Latency was driven from 7s down to **~1.2–1.7s**. The remaining
quality gap is the **voice** (Piper sounds robotic); fixing it means running Kokoro
on a GPU. That hosting step is Phase 2 — specified in `SPEC.md` next to this file.

---

## 1. What this project is

An inbound phone call AI: you dial a Telnyx number, an AI assistant answers and
holds a natural spoken conversation. Every audio/AI component is open-source and
self-hosted — **no ElevenLabs, Deepgram, Vapi, or Retell**. The only external
costs are Telnyx (telephony) and the Claude API (the reasoning brain).

The pipeline per turn:
```
caller speech ─▶ Silero VAD (end-of-turn) ─▶ faster-whisper STT ─▶ Claude (streaming)
            ─▶ sentence chunker ─▶ Kokoro/Piper TTS ─▶ μ-law ─▶ back to caller
barge-in: if the caller talks while Aria is speaking, we send Telnyx a "clear"
          event to flush playback and start a fresh listen cycle.
```

## 2. Where everything lives

- **App code:** `~/voice-agent/` (this folder's parent). Run with `make run`.
- **Phase-2 (hosting) space:** `~/voice-agent/deploy/` (this folder).
  - `SPEC.md` — the Phase-2 hosting spec (READ THIS to know what to do next).
  - `SESSION-HANDOFF.md` — this document.
  - `.env` — **secrets already extracted here** (Anthropic + Telnyx keys), gitignored.
  - `Dockerfile.gpu` — a starting-point GPU container.
  - `cloudflared-named-tunnel.md` — how to get a stable public `wss://` (free).
- **Validated architecture decisions + measured latency:** `~/voice-agent/ARCHITECTURE_VALIDATION.md`.

### Code map (every file < 300 lines, modular by design)
```
main.py                  FastAPI app + lifespan (loads models once)
config.py                ALL tunable knobs + Aria's persona/prompt
app/runtime.py           VAD/STT/TTS singletons + warmup
app/session.py           per-call state (history + audio sinks)
app/audio/codec.py       μ-law<->PCM, resample 8k/16k/24k (stdlib audioop)
app/audio/vad.py         Silero VAD + utterance segmenter (end-of-turn detection)
app/audio/stt.py         faster-whisper wrapper
app/audio/tts.py         TTS wrapper — Kokoro (PyTorch) primary, Piper fallback
app/agent/brain.py       Claude streaming -> text deltas
app/agent/sentence_chunker.py   token stream -> complete sentences
app/agent/pipeline.py    per-call orchestration + barge-in + timing logs
app/telephony/protocol.py   Telnyx media-WS JSON frames (parse/build)
app/telephony/commands.py   Telnyx Call Control REST (answer, streaming_start)
app/telephony/webhook.py    POST /telnyx/webhook (call lifecycle)
app/telephony/media_ws.py   WS /ws/media (bidirectional audio)
scripts/offline_smoke.py    full local pipeline check, no phone
scripts/setup_telnyx.py     idempotent: wire a number to this agent
scripts/start.sh            tunnel + Telnyx wiring + server, one command
```

## 3. How it was validated (what "proven" means)

1. **Credentials, live:** Telnyx key (3 US numbers; `+15551234567` dedicated to the
   agent) and Anthropic key both verified with real API calls before any code.
2. **Offline smoke (`make smoke`):** TTS→μ-law→STT round-trip returned the input
   text verbatim; VAD detected utterance boundaries; Claude streamed sentences.
3. **Live phone calls:** multiple real calls to `+15551234567`. Aria greeted,
   transcribed the caller, replied via Claude, and barge-in worked
   (caller talking over Aria cut her off). Confirmed end-to-end on PSTN.

## 4. The latency story (this is the heart of the session)

Measured on this box (WSL2, **CPU-only**, 10 cores, 9.7 GB RAM):

| Stage | First call | After fixes |
|---|---|---|
| VAD silence tail | 800 ms | 600 ms |
| STT | ~1000 ms (`small.en`) | 480–740 ms (`tiny.en`) |
| Claude first sentence | ~600 ms | ~600–720 ms |
| **TTS first sentence** | **~2000 ms (Kokoro/PyTorch)** | **82–265 ms (Piper)** |
| **Total to first audio** | **~3.4–7 s** | **~1.2–1.7 s** |

What caused the 7s and how it was fixed:
- **Bug:** `torch.set_num_threads(1)` (added for Silero) throttled **all** torch ops
  — including Kokoro — to a single core. Removed → big TTS speedup.
- **STT:** `small.en` cost scales with utterance length; switched to `tiny.en` and
  set ctranslate2 `cpu_threads`. (Accuracy drop: `tiny.en` misheard "Corpus" as
  "crops". `base.en` is the accuracy/speed compromise.)
- **TTS — the key finding:** PyTorch Kokoro ≈ 2s/sentence on CPU. Switched to
  **Piper** (≈ 100–300 ms/sentence) to hit the latency target. Piper is the
  reason it's fast — and the reason it sounds robotic.
- **Claude API itself was never the bottleneck:** measured ~520 ms to first token.
- **Warmup:** one dummy inference per model at startup removes cold-start on turn 1.

### Why Phase 2 exists (the Kokoro-on-CPU investigation)
The user wants Kokoro's natural voice back. Before assuming a GPU is required, this
session benchmarked **`kokoro-onnx`** (ONNX Runtime) on this same CPU:
- fp32 ONNX: **~0.6× real-time** (1–2 s/sentence) — still too slow.
- int8 ONNX: **~1.5× real-time** (slower! int8 isn't well-optimized on this CPU).

**Conclusion (data-backed):** Kokoro quality at conversational latency needs a GPU
(or possibly a much faster CPU — unverified). On a cheap GPU, Kokoro runs
~10–50× real-time (~50–150 ms/sentence) → quality *and* speed. That is Phase 2.

## 5. Current runtime config (Phase 1, CPU)

`~/voice-agent/.env` currently set to the fast CPU profile:
```
WHISPER_MODEL=tiny.en
TTS_ENGINE=piper
PIPER_MODEL_PATH=voices/en_US-amy-medium.onnx
MODEL=claude-haiku-4-5
```
Engines are swappable purely by env — Phase 2 flips `TTS_ENGINE=kokoro`,
`WHISPER_MODEL=small.en`, `WHISPER_DEVICE=cuda` (see SPEC.md §4 for the one code
addition needed: a GPU-aware Kokoro engine in `app/audio/tts.py`).

## 6. How to run it right now

```bash
cd ~/voice-agent
make smoke          # offline sanity (no phone)
make run            # opens cloudflared tunnel, wires Telnyx, starts server
                    # -> prints "Call this number to reach Aria: +15551234567"
```
The cloudflared **quick tunnel** URL is ephemeral (new every run); `start.sh`
re-points the Telnyx Call Control app webhook automatically each run. Phase 2
replaces this with a stable endpoint (SPEC.md §5).

To stop a backgrounded run: `pkill -f "uvicorn main:app"; pkill -x cloudflared`.

## 7. Known gaps / cleanups (carry into Phase 2)
- **Debug logs:** `[timing]` / `[pipeline]` prints are always-on. Gate behind a
  `DEBUG` flag (or logging level) before prod.
- **Single-call assumption:** engines are shared singletons; concurrent calls
  share them and GPU inference serializes. Fine for 1–few calls; see SPEC §6.
- **Telnyx app is shared dev:** Phase 1 reuses one Call Control app and rewrites
  its webhook each run. Phase 2 should create a stable prod app.
- **No auth on the media WS / webhook:** add Telnyx webhook signature verification
  and restrict the media WS in prod (SPEC §7).
- **Git:** `~/voice-agent` is a git repo with no commits yet. Nothing pushed.

## 8. Secrets
Already extracted into `deploy/.env` (and present in `~/voice-agent/.env`):
- `ANTHROPIC_API_KEY` (from `~/agent-orchestrator/.env`)
- `TELNYX_API` (from `~/web-portal/.env`)
- Telnyx number for the agent: `+15551234567`.
Do **not** commit `.env` or bake keys into images — both `.gitignore`s exclude it.
```
