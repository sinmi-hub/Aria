# Aria — a self-hosted voice AI phone agent

Aria is a production-grade **voice AI agent that answers and makes real phone calls** over the
PSTN. It was built as an after-hours sales agent (it qualifies leads, answers product questions,
and books demos), but the core is a general, reusable voice-agent framework.

The design goal is **self-hosted-first**: the only mandatory paid services are **telephony**
(Telnyx) and a **reasoning LLM**. Speech-to-text (faster-whisper), voice-activity detection
(Silero), and text-to-speech (Kokoro) all run locally — no ElevenLabs, Deepgram, Vapi, or Retell
required. An optional OpenAI Realtime backend is included for the lowest latency.

What makes this repo unusual is the **observability**: it ships a ground-truth latency oracle and an
honest, reproducible benchmark of two architectures (see [Latency & metrics](#latency--metrics)).
The numbers are measured from the call audio itself, not self-reported by the app.

---

## Two architectures, one flag

Aria can run the same persona, tools, and telephony through either pipeline. Select with
`REALTIME_BACKEND`:

### 1. Local cascade (`REALTIME_BACKEND=local`, default)

```
PSTN ─▶ Telnyx ─▶ POST /telnyx/webhook              (answer ▶ streaming_start)
                       │
            μ-law 8k ⇄ WS /ws/media
                       │
  inbound ▶ Silero VAD ▶ faster-whisper ▶ BRAIN (stream) ▶ sentence chunker
                                                ▶ Kokoro TTS ▶ μ-law ▶ caller
  barge-in: caller talks over Aria ▶ {"event":"clear"} flushes playback
```

The **brain** is itself selectable with `BRAIN_BACKEND`:
- `claude` (default) — Anthropic Claude over the network (~600ms TTFT).
- `slm` — a **local Qwen3-4B** running on the same GPU (transformers + CUDA). Includes
  streaming STT→LLM overlap and KV-cache pre-warming. See
  [`deploy/PHASE7-CASCADE-IMPLEMENTATION.md`](deploy/PHASE7-CASCADE-IMPLEMENTATION.md).

### 2. OpenAI Realtime (`REALTIME_BACKEND=openai`)

A fused speech-to-speech bridge (`app/telephony/realtime_bridge.py`) that streams μ-law straight to
the OpenAI Realtime API and back. Same tools, same persona, lowest latency. This is the production
path for raw speed.

Both backends emit the **same trace events**, so `/debug/turns` and the scorecard work identically.

---

## Capabilities

- **Real phone calls** in and out, over Telnyx (g711 μ-law 8 kHz, dual-channel recording).
- **Tool use** — seven sales tools backed by Google Sheets (a lightweight lead CRM) and Google
  Calendar (demo booking): `lookup_lead`, `find_open_slots`, `book_meeting`, `reschedule_meeting`,
  `log_call`, `flag_for_human`, `mark_do_not_call`.
- **Natural turn-taking** — content-aware barge-in, backchannel detection ("mm-hm" doesn't
  interrupt), coalescing of rapid corrections, and a latency-triggered micro-ack that covers LLM
  spikes without sounding robotic.
- **Compliance** — recorded-call announcement (TCPA), do-not-call handling, $/day outbound cap.
- **Self-configuring deploy** — a golden Docker image that boots on a RunPod GPU, opens a
  cloudflared tunnel, and wires Telnyx to itself with no manual step.

---

## Latency & metrics

Aria treats latency as a first-class, measured property. Two layers:

1. **In-app trace** (`app/trace.py` → `/debug/turns`): per-stage timing (VAD tail, STT, LLM TTFT,
   sentence chunk, TTS) for every turn.
2. **Ground-truth oracle** (`scripts/call_timing.py`): pulls the Telnyx **dual-channel recording**,
   runs **Silero VAD** on each leg (the standard benchmark method), and measures the real
   **caller-stop → agent-audio** gap — independent of what the app claims about itself. It reports
   two metrics: **time-to-first-sound** and **time-to-content**.

The honest finding, measured apples-to-apples (full write-up in
[`deploy/PHASE5-SCORECARD.md`](deploy/PHASE5-SCORECARD.md)):

| backend | perceived median (audio oracle) |
|---|---|
| OpenAI Realtime | **~1.25 s** |
| Local cascade (Qwen SLM) | **~2.0 s** |

The fused realtime model wins on latency by ~750 ms; the self-hosted cascade's case is
cost/control, not speed. The local SLM has a fast TTFT (~450 ms), but the cascade's extra stages
(chunking, TTS, separate hops) erase that lead. The methodology — and where the app's own number
*understates* perceived latency — is documented honestly, including a hypothesis the data later
killed and a retraction.

```bash
# measure any call from its recording
python scripts/call_timing.py list
python scripts/call_timing.py time latest          # or a recording id / mp3 path
```

---

## Quick start (local, no phone)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in ANTHROPIC_API_KEY (+ TELNYX_API for real calls)

python -m scripts.offline_smoke   # exercises VAD ▶ STT ▶ brain ▶ TTS with no phone
```

To take live calls you need a Telnyx number and a public URL (a cloudflared tunnel works):

```bash
bash scripts/start.sh          # opens a tunnel, wires Telnyx, starts the server
```

## Deploy (RunPod GPU)

```bash
python deploy/runpod_up.py --image docker.io/<you>/voice-agent:latest --no-validate
```

The image self-wires Telnyx from `deploy/.env`. See [`deploy/`](deploy/) for the full runbooks
(`SPEC.md`, `bare-metal-runbook.md`, the phase notes).

---

## Configuration

All knobs live in `config.py` with env overrides; secrets come only from the environment (loaded
from `.env`, which is git-ignored). See `.env.example` for the full list. Highlights:

| var | default | meaning |
|---|---|---|
| `REALTIME_BACKEND` | `local` | `local` cascade or `openai` realtime |
| `BRAIN_BACKEND` | `claude` | `claude` or local `slm` (GPU) |
| `MODEL` | `claude-haiku-4-5` | reasoning model for the Claude brain |
| `WHISPER_MODEL` | `small.en` | faster-whisper model |
| `KOKORO_VOICE` | `af_heart` | local TTS voice |
| `VAD_SILENCE_THRESHOLD_MS` | `300` | end-of-turn silence |

The Google integration (Sheets CRM + Calendar) is optional — without credentials the agent runs
text-only and still answers calls. See `deploy/google-service-account-setup.md`.

---

## Layout

```
main.py                 FastAPI app + lifespan (loads engines once) + /debug/* endpoints
config.py               all tunable knobs + persona (single source of truth)
app/
  runtime.py            VAD / STT / TTS / SLM singletons
  trace.py              structured per-turn timing events
  audio/                codec · vad (Silero) · stt (faster-whisper) · tts (Kokoro) · streaming_stt
  agent/                brain (Claude) · slm_brain (Qwen) · slm_common · sentence_chunker · pipeline · tools · persona
  telephony/            protocol · commands · webhook · media_ws · realtime_bridge
  integrations/         google_client · crm (Sheets) · gcal (Calendar)
scripts/
  offline_smoke.py      full local pipeline check (no phone)
  call_timing.py        ground-truth latency oracle (Silero, dual-channel)
  realtime_probe.py     OpenAI Realtime latency probe
  slm_probe.py          local SLM TTFT + tool-calling probe
  setup_telnyx.py       wire a number to this agent (idempotent)
deploy/                 Dockerfiles, RunPod bring-up, runbooks, phase notes + scorecard
```

---

## Security

- **No secrets are committed.** `.env`, `.keys/`, and service-account JSON are git-ignored; the
  repository history has been verified clean. Only `.env.example` (placeholders) is tracked.
- A **secret-blocking pre-commit hook** ships in `.githooks/` — enable it once after cloning:
  ```bash
  git config core.hooksPath .githooks
  ```
  It refuses any commit containing an API-key-shaped string.

---

## License

MIT — see [LICENSE](LICENSE).
