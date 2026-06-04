# Aria, a self-hosted voice AI phone agent

Aria answers and makes real phone calls over the PSTN. It was built as an after-hours sales agent
(it qualifies leads, answers product questions, and books demos), but the core is a general,
reusable voice-agent framework.

The design goal is self-hosted first. The only mandatory paid services are telephony (Telnyx) and a
reasoning LLM. Speech-to-text (faster-whisper), voice-activity detection (Silero), and text-to-speech
(Kokoro) all run locally, with no ElevenLabs, Deepgram, Vapi, or Retell. An optional OpenAI Realtime
backend is included for the lowest latency.

What makes this repo unusual is the observability. It ships a ground-truth latency oracle and an
honest, reproducible benchmark of two architectures, measured from the call audio itself rather than
self-reported by the app.

## Two architectures, one flag

Aria runs the same persona, tools, and telephony through either pipeline. Select with
`REALTIME_BACKEND`.

**Local cascade** (`local`, the default):

```
PSTN > Telnyx > POST /telnyx/webhook            (answer, then streaming_start)
                   |
        mu-law 8k <> WS /ws/media
                   |
  inbound > Silero VAD > faster-whisper > BRAIN (stream) > sentence chunker
                                              > Kokoro TTS > mu-law > caller
  barge-in: caller talks over Aria, {"event":"clear"} flushes playback
```

The brain is itself selectable with `BRAIN_BACKEND`: `claude` (default, Anthropic over the network)
or `slm` (a local Qwen3-4B on the same GPU, with streaming STT overlap and KV-cache pre-warming).

**OpenAI Realtime** (`openai`): a fused speech-to-speech bridge that streams mu-law straight to the
OpenAI Realtime API and back. Same tools, same persona, lowest latency.

Both backends emit the same trace events, so `/debug/turns` and the scorecard work identically.

## Capabilities

- Real phone calls in and out over Telnyx (g711 mu-law 8 kHz, dual-channel recording).
- Seven sales tools backed by Google Sheets (lead CRM) and Google Calendar (booking): `lookup_lead`,
  `find_open_slots`, `book_meeting`, `reschedule_meeting`, `log_call`, `flag_for_human`,
  `mark_do_not_call`.
- Natural turn-taking: content-aware barge-in, backchannel detection, coalescing of rapid
  corrections, and a latency-triggered micro-ack that covers LLM spikes.
- Compliance: recorded-call announcement (TCPA), do-not-call handling, daily outbound cap.
- Self-configuring deploy: a golden Docker image boots on a RunPod GPU, opens a cloudflared tunnel,
  and wires Telnyx to itself.

## Latency and metrics

Aria treats latency as a first-class, measured property, on two layers. The in-app trace
(`app/trace.py`, exposed at `/debug/turns`) records per-stage timing for every turn. The ground-truth
oracle (`scripts/call_timing.py`) pulls the Telnyx dual-channel recording, runs Silero VAD on each
leg (the standard benchmark method), and measures the real caller-stop to agent-audio gap
independently of the app, reporting both time-to-first-sound and time-to-content.

![Voice agent latency explainer](docs/latency-explainer.png)

Measured apples-to-apples (full write-up in `deploy/PHASE5-SCORECARD.md`):

| backend | perceived median (audio oracle) |
|---|---|
| OpenAI Realtime | ~1.25 s |
| Local cascade (Qwen SLM) | ~2.0 s |

The fused realtime model wins on latency by about 750 ms. The self-hosted cascade's case is cost and
control, not speed: the local SLM has a fast TTFT (~450 ms), but the cascade's extra stages
(chunking, TTS, separate hops) erase that lead.

```bash
python scripts/call_timing.py list
python scripts/call_timing.py time latest      # or a recording id / mp3 path
```

## Quick start (local, no phone)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # set ANTHROPIC_API_KEY (and TELNYX_API for real calls)
python -m scripts.offline_smoke     # VAD > STT > brain > TTS, no phone needed
```

For live calls you need a Telnyx number and a public URL. A cloudflared tunnel works:

```bash
bash scripts/start.sh               # opens a tunnel, wires Telnyx, starts the server
```

## Deploy (RunPod GPU)

```bash
python deploy/runpod_up.py --image docker.io/<you>/voice-agent:latest --no-validate
```

The image self-wires Telnyx from `deploy/.env`. See `deploy/` for the full runbooks.

## Configuration

All knobs live in `config.py` with env overrides. Secrets come only from the environment (loaded
from `.env`, which is git-ignored). See `.env.example` for the full list.

| var | default | meaning |
|---|---|---|
| `REALTIME_BACKEND` | `local` | `local` cascade or `openai` realtime |
| `BRAIN_BACKEND` | `claude` | `claude` or local `slm` (GPU) |
| `MODEL` | `claude-haiku-4-5` | reasoning model for the Claude brain |
| `WHISPER_MODEL` | `small.en` | faster-whisper model |
| `KOKORO_VOICE` | `af_heart` | local TTS voice |
| `VAD_SILENCE_THRESHOLD_MS` | `300` | end-of-turn silence |

The Google integration is optional. Without credentials the agent runs text-only and still answers
calls.

## Layout

```
main.py        FastAPI app, lifespan, /debug/* endpoints
config.py      all tunable knobs and persona
app/
  runtime.py   VAD / STT / TTS / SLM singletons
  trace.py     structured per-turn timing
  audio/       codec, vad, stt, tts, streaming_stt
  agent/       brain, slm_brain, slm_common, sentence_chunker, pipeline, tools, persona
  telephony/   protocol, commands, webhook, media_ws, realtime_bridge
  integrations/ google_client, crm (Sheets), gcal (Calendar)
scripts/
  offline_smoke.py   full local pipeline check
  call_timing.py     ground-truth latency oracle
  realtime_probe.py  OpenAI Realtime probe
  slm_probe.py       local SLM probe
deploy/        Dockerfiles, RunPod bring-up, runbooks, scorecard
```

## License

MIT, see [LICENSE](LICENSE).
