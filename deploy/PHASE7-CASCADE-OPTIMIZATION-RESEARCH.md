# Cascade voice-agent latency optimization — research report (2026-06-02)

Deep-research output (26 sources fetched → 129 claims → 25 adversarially verified, 3-vote;
16 confirmed, 9 killed). Question: **can a self-hosted STT→LLM→TTS cascade be competitive
enough to avoid OpenAI Realtime, and what moves the numbers?** Context = Aria: Telnyx 8kHz
μ-law, faster-whisper STT, Claude (~600ms TTFT) / Qwen3-4B SLM (~376ms TTFT), sentence
chunker, Kokoro TTS, single RTX A4500 (20GB).

## Verdict

**Yes — a fully-streaming, co-located cascade can hit sub-1s and is a viable alternative to
realtime**, but only with stage overlap. Sequential cascades run **2–4s**; streaming cascades
hit **sub-1s**. Strongest proof: Apple's **ChipChat** (arXiv 2509.00078, Sep 2025) achieved
**sub-second voice-to-voice on a Mac Studio with NO discrete GPU**, entirely on-device. Realtime
still wins raw latency (<500ms) + prosody; the cascade wins tool-calling reliability,
observability, voice control, and cost. (This matches our own park decision — realtime for raw
speed; cascade is the self-hosted alternative if vendor lock/cost/voice-control dominate.)

## Confirmed findings (3-vote verified)

1. **LLM TTFT is the dominant + most variable stage** (STT/TTS are smaller and stable). HF
   production playbook: LLM TTFT **P50 566ms / P95 2,246ms** (~4× spread) vs endpointing P50 554 /
   P95 858 and TTS TTFB P50 243 / P95 296. "The LLM is where variance lives." → the primary lever.
2. **Sequential = 2–4s; streaming across all 3 stages = sub-1s.** LiveKit's streaming budget:
   transport <50ms, STT first-partial 100–200ms, **LLM TTFT 200–400ms** (aggressive ideal),
   TTS first-audio 100–300ms → <1s *target* (not a guaranteed median). Sayna measured a real
   streaming pipeline at **755ms TTFA** (self-host best 729ms).
3. **A streaming cascade can be sub-second even without a GPU** (ChipChat, M2 Ultra, on-device):
   ASR ~165–175ms, LLM ~576ms, TTS ~880ms-after-5-words → ~920ms to first audio. Overturns the
   "cascade is inherently too slow" assumption — directly relevant to our co-location question.
4. **The core mechanism = streaming overlap + KV-cache warmth:** (a) pre-encode the prompt
   *before* the call; (b) stream ASR tokens to the LLM as generated; (c) stream LLM tokens to TTS
   as generated; (d) rotating KV cache; (e) **start TTS after only ~5 words**. Pipecat/Nemotron
   confirms sentence-boundary chunking with `first_segment_max_tokens≈24`, `segment_hard_max≈96`.
5. **Semantic/eager turn detection cuts 150–600ms** of perceived latency by removing the
   sequential endpointing step + triggering the LLM early. Deepgram **Flux**: 200–600ms saved,
   ~30% fewer false interruptions. **Eager EOT** (threshold 0.3–0.5): fires 150–250ms earlier
   **at the cost of 50–70% more LLM calls** (speculative, discarded on resume). LiveKit's turn
   detector = **Qwen2.5-0.5B** on CPU (~281MB, ~50–160ms/turn), 39% fewer false interrupts.
6. **Tool-call turns are the worst case — a two-pass penalty.** Two silences (P50 **~1.7s** then
   **~1.4s**); the first pass is slower (**913ms** vs 586ms) because it carries full context +
   tool defs. Mitigations: **micro-ack/filler**, **concurrent/speculative tool exec at turn
   start** ("if you know the call in advance, make it concurrently"), parallel tool calls.
7. **Target window is right:** >~1.5s rapidly degrades; natural human gap ~200ms; ~300ms already
   feels slightly unnatural (Cresta, AssemblyAI 300ms rule, Stivers 2009 PNAS). Typical production
   **median 1.4–1.7s sits in the degradation zone** — which is *why* streaming is mandatory.
8. **S2S vs cascade (medium confidence, 2-1):** S2S <500ms + better prosody (operates on audio,
   no text bottleneck), but degrades on long calls (~5–6s/turn after 5 min reported); cascade wins
   tools/observability/control/cost. Well-optimized self-hosted cascade = viable alternative.

## Killed in verification — DO NOT rely on these

The 3-vote panel refuted these; treat as **unverified, measure ourselves**:
- vLLM vs TensorRT-LLM vs SGLang **TTFT engine ranking** + concurrency tail-latency numbers (0-3).
- "**4-bit quantization = 40% latency cut, 95% quality**" (0-3). *(Our own probe agrees: 4-bit was
  slower than bf16 on the A4500.)*
- "**100% KV reuse → ~0ms context**" on single-slot (0-3).
- "0.94s self-hosted total" and "~500–700ms self-hosted cascade" (split 1-2, not robust).
- The "1.39s P50 typical production median" specific figure (1-2) — directionally ~1.4–1.7s holds
  from other sources, but that exact datapoint didn't survive.

## Open questions (need direct measurement on Aria's hardware)

1. **Single-GPU contention** when faster-whisper + Qwen3-4B + Kokoro are co-located on the A4500
   under concurrent load — VRAM headroom, TTFT degradation. (We already have `/debug/gpu` +
   per-stage trace = exactly the tooling to answer this.)
2. **Routing: upfront classifier vs try-SLM-then-escalate** — no source quantified the escalation
   double-pay vs classifier-error tradeoff. (Our reasoning already favors upfront classification.)
3. **Best inference engine for a 4B on the A4500** (vLLM / TensorRT-LLM / SGLang / llama.cpp/MLX)
   — published rankings were refuted; measure single-request TTFT directly.
4. **Eager-EOT / barge-in cost on 8kHz μ-law telephony** specifically — low fidelity may degrade
   the turn detector vs the wideband audio in the benchmarks.

## What would move Aria's numbers most (actionable, ranked)

1. **Stream STT→LLM (biggest unrealized win).** Today faster-whisper transcribes the *whole*
   utterance before the LLM starts — that's the sequential penalty (finding #2). Feed partial
   transcripts / chunked decode so the LLM prefills while the caller is still finishing. This is
   the gap between our ~790ms projection and a real streaming sub-1s.
2. **Pre-warm the SLM KV cache** (finding #4): pre-encode the (static) system prompt + tools at
   call start and reuse the slot, so per-turn TTFT is just the new tokens. We control this locally
   (unlike Claude, where caching was inert under the 2048-token floor).
3. **Keep + tune the LLM→TTS sentence chunking** (we already have it; `_MIN_CHARS=7`). Consider
   clause-level first-segment (~first 24 tokens / 5 words) to shave time-to-first-audio.
4. **Semantic/eager endpointing** to replace the fixed 300ms VAD tail (finding #5) — but validate
   on μ-law telephony (open Q4) and budget the 50–70% extra speculative LLM calls.
5. **Attack tool-turn latency** (finding #6 — most of a sales call): micro-ack (have it) +
   **speculative/concurrent tool exec** + **upfront routing** (avoid the escalation double-pay).
6. **Co-location is already the right call** (network-hop elimination, finding #3/ChipChat) —
   the risk is single-GPU contention; monitor via `/debug/gpu` + trace (open Q1).

## Sources (primary first)

- HuggingFace voice-agent latency playbook (production P50/P95): huggingface.co/blog/dvalle08/voice-agent-latency-playbook
- ChipChat, Apple (sub-1s cascade, on-device): arxiv.org/pdf/2509.00078
- Telecom voice-agent paper (per-stage measured): arxiv.org/html/2508.04721v1
- Cresta — engineering real-time voice latency: cresta.com/blog/engineering-for-real-time-voice-agent-latency
- LiveKit — architecture / budgets: livekit.com/blog/voice-agent-architecture-stt-llm-tts-pipelines-explained
- LiveKit — end-of-turn model: livekit.com/blog/improved-end-of-turn-model-cuts-voice-ai-interruptions-39
- Deepgram Flux: deepgram.com/learn/introducing-flux-conversational-speech-recognition
- Pipecat/Nemotron streaming pipeline: github.com/pipecat-ai/nemotron-january-2026 (docs/streaming-pipeline-architecture.md)
- getstream — speculative tool calling: getstream.io/blog/speculative-tool-calling-voice
- webrtc.ventures — parallel SLM/LLM: webrtc.ventures/2025/06/reducing-voice-agent-latency-with-parallel-slms-and-llms
- Coval — S2S vs cascade: coval.ai/blog/speech-to-speech-vs-cascaded-voice-ai-which-architecture-should-you-deploy

**Caveat:** several headline figures are vendor self-reported (Deepgram 200–600ms, LiveKit 39%,
OpenAI Realtime <500ms) — directional, not neutral benchmarks. Absolute LLM ms are model/hardware
specific (2B/H100 ≠ Aria's Claude-network ≠ Qwen3-4B/A4500); the *conclusion* (LLM is the lever,
streaming gets sub-1s) is robust, the exact ms are not. Field is fast-moving (2025–2026).
