# Phase 7 (candidate) — SLM + Claude cascade: tool-calling SLM research

**Status:** research only (2026-06-02). Not started. Decision input for the cascade idea:
local SLM as the default brain, Claude (Haiku) as the escalation path, a fast classifier
routing between them. Goal: **< 1s TTFA while staying self-hosted** (any voice incl. Kokoro,
no vendor lock, Claude-grade reasoning only when needed).

## Why a cascade (vs OpenAI realtime)

- Realtime wins raw latency (~333ms live) but locks us to OpenAI voices + per-minute cost.
- A local SLM on the A4500 would cut TTFT from ~600ms (Claude over network) to ~100ms for the
  turns it handles → ~750–850ms TTFA. Not as fast as realtime, but **under 1s and fully ours.**
- The crux is **coverage + tool-calling**: many of our turns are tool turns (lookup_lead,
  find_slots, reschedule, log_call). If the SLM can't do tools reliably, those escalate to
  Claude and the win shrinks. So the SLM MUST be good at function-calling.

## Benchmark findings (BFCL + a practical 13-model local eval)

Canonical benchmark = Berkeley Function-Calling Leaderboard (BFCL). Large leaders: GLM-4.5
(76.7%), Qwen3-32B (75.7%) — too big. For SMALL models (fit the A4500 alongside whisper
~1.5GB + kokoro ~0.4GB; 20GB total), a hands-on eval (40 cases, 5 categories, real endpoints):

| Model | Size (Q4) | Tool-call pass | Notes |
|---|---|---|---|
| **Qwen3.5 4B** | **3.4 GB** | **97.5%** | top; over-parallelizes sometimes; clear lead |
| Nemotron Nano 4B | 4.2 GB | 95.0% | strong sequential multi-turn; good fallback |
| Qwen3 8B | 5 GB | 85.0% | solid budget option |
| Mistral Nemo 12B | 7.5 GB | 92.5% | best at sequential, but slower / bigger |
| xLAM-2 8B, Hammer, Mistral Small | various | 15–57% | **chat-template/endpoint incompat**, not true model limits |

**Key caveats:**
- xLAM-2 is #1 on official BFCL but cratered in the practical eval purely due to **chat-template
  incompatibility** with the serving stack. Lesson: *leaderboard rank ≠ works-on-our-stack.*
  Validate on OUR prompt format + tool schemas before trusting any number.
- Those scores are single-turn function-calling. BFCL **multi-turn** is much lower for everyone
  — but our turns are mostly a single tool call, so single-turn FC is the right metric.
- Throughput figures (~48 tok/s) are someone's consumer GPU; what matters for us is **TTFT
  (first-token)**, which a 4B on the A4500 should hit in ~50–150ms. Must measure.

## Recommendation

**Primary candidate: Qwen3.5 4B (Q4_K_M).** Best tool-calling at the smallest size, trivial VRAM
footprint next to whisper+kokoro, fast. Fallback: Nemotron Nano 4B. Budget: Qwen3 8B.

## Next step — SLM probe (de-risk before building the router)

Mirror the realtime probe that just paid off. On the pod, load Qwen3.5 4B (Q4) and measure:
1. **TTFT (first-token)** on the A4500 — is the ~100ms win real?
2. **Tool-calling reliability on OUR schemas** — feed ~6 representative turns (lookup_lead by
   phone, find_open_slots, reschedule by number, a pricing chat turn, a backchannel) and check
   it emits the correct call/args, in our serving format.
3. **VRAM coexistence** with whisper-medium + kokoro loaded.

If (1) and (2) hold → design the classifier (start heuristic: tool-likely/ambiguous → Claude;
chat/ack/FAQ → SLM) and the router. If tool-calling is shaky → SLM carries chat-only and the
win is diluted (decide if still worth it).

### Probe BUILT (2026-06-03) — `scripts/slm_probe.py` (+ `scripts/test_slm_probe.py`)

Mirrors `realtime_probe.py`. Measurement-only, no pipeline wiring. It loads one candidate
SLM via `transformers` and, using the model's OWN chat template with our REAL tool schemas
(`app.agent.tools.SCHEMAS`) and REAL persona (`app.agent.persona.system_prompt`), runs the
6-turn de-risk corpus and reports per-case tool-call correctness + first-token TTFT, plus
whole-GPU snapshots (via `app.gpu`) before load / after load / after generation.

- Pure-logic parts (tool-call parsing — Hermes `<tool_call>` + bare-JSON fallback — and
  case scoring) are covered by `test_slm_probe.py` and pass locally. GPU load/generate runs
  only on the pod.
- Why the model's native template (not vLLM/llama.cpp): faithful upper-bound test of the
  model's tool-calling. If it fails here it's hopeless; if it passes, validate the actual
  serving stack separately. (The notes' core warning: leaderboard rank ≠ works-on-our-stack.)

**Run it on the pod** (needs the GPU; pod has ssh via `runpod_up.py --ssh`, key
`~/.ssh/voiceagent_rp`):
```bash
ssh -i ~/.ssh/voiceagent_rp -p <port> root@<ip> \
  'cd /app && pip3 install -q -U transformers accelerate bitsandbytes && \
   python3 -m scripts.slm_probe --model Qwen/Qwen3-4B --dtype 4bit'
```
- `--model`: **confirm the exact HF repo id** for the point release ("Qwen3.5 4B" in the
  table above may be `Qwen/Qwen3-4B` or a newer tag). `--dtype 4bit` matches the ~3.4GB
  production footprint (needs bitsandbytes); `--dtype bf16` is the no-bnb fallback (~8GB).

**Coexistence caveat (item 3):** the current live pod is **phase6d = realtime backend**, which
does NOT load whisper/kokoro (OpenAI handles STT/TTS), so its GPU snapshot is NOT the cascade
scenario. To measure true coexistence, run the probe alongside a **local-backend** server
(`REALTIME_BACKEND=local`, whisper+kokoro resident) — `app.gpu` reads the whole GPU via
nvidia-smi, so a second ssh session running the probe will show the combined footprint.

### Probe RESULTS (2026-06-03) — `Qwen/Qwen3-4B-Instruct-2507`, image `gpu-phase7`

Ran on the A4500 over `/debug/slm_probe` (image pod, no SSH), whisper+kokoro resident.
Used the **Instruct-2507** non-thinking variant (the original `Qwen3-4B` is hybrid-thinking
→ `<think>` blocks would wreck phone-turn TTFT). Native chat template + our real schemas/persona.

| metric | bf16 raw | bf16 **tuned** | 4-bit (nf4) |
|---|---|---|---|
| TTFT first-token (median / p95) | 369 / 377 ms | **376 / 380 ms** | 468 / 480 ms |
| tool-calling (6-turn corpus) | 4/6 | **5/6** | 3/6 |
| VRAM after load (with whisper+kokoro) | 10027 MB | 10027 MB | 10775† |

**Tuned prompt (`gpu-phase7b`, SLM-path-only fixes in `slm_probe.py`):** fixed `lookup_lead`
(strip lead-in rule + "emit tools immediately") and `reschedule` (sharper descriptions) → 5/6.
New miss `find_slots`→`lookup_lead` is a harness artifact (every case reuses the forceful
"look them up right away" inbound context; benign mid-call). TTFT unchanged → the directive is free.

†4-bit VRAM contaminated: the endpoint loads a fresh model per run without freeing the prior
one in-process, so the 2nd run's GPU delta is unreliable. bf16's (first run) is clean.
Baseline before any SLM = 2453 MB (whisper `medium.en` + kokoro). Util ~77% during one bf16 gen.

**What changed vs the research above:**
- **bf16 > 4-bit on this GPU** for both latency (~100 ms less TTFT) and tool-calling. VRAM is
  not the constraint (10 GB free at bf16), so the "Q4 / 3.4 GB" pick is **superseded → run bf16.**
- **TTFT win confirmed** (~369 ms vs Claude ~600 ms). Cascade TTFA ≈ **~780 ms (< 1s, self-hosted)**;
  still slower than realtime ~333 ms first-audio.
- **Tool-calling 4/6**, two addressable misses: lead-in-then-yield on `lookup_lead` (our persona's
  "say a line first" rule — drop it on the SLM path), and `book_meeting` chosen over
  `reschedule_meeting` (sharpen tool descriptions). Negative cases (pricing, backchannel) correct.

**Prompt fixes done → re-probed 5/6 (2026-06-02, `gpu-phase7b`).** Fixed lookup_lead +
reschedule; new miss find_slots→lookup is a harness artifact. TTFT unchanged (~376ms).

**Latency caveat (important — the ~790ms is best-case):** that projection is a CHAT turn (first
token streams straight to TTS). A TOOL turn can't stream the tool call into speech — the SLM must
fully generate the `<tool_call>` block, run the tool (~170–370ms), then a SECOND pass produces the
spoken answer → ≈ **1.3–1.6s+ ex-VAD per single-tool turn**, stacking on multi-tool turns. The
micro-ack masks *perceived* latency, not actual. Realtime also pays tool latency but with a faster
fused model. So the cascade's latency edge is real on chat turns and thin/negative on tool turns —
which is most of a sales call.

**Escalation double-pay (the clinching argument against it):** the cascade only wins if the
router is a cheap, accurate UPFRONT classifier that sends hard turns straight to Claude (Claude
turn ≈ Claude-alone, ~600–660ms TTFT). Realistically you don't know a turn is hard until the SLM
flubs it (try-SLM-then-escalate) → that turn pays SLM attempt (~376–800ms) + routing + Claude
(~600ms), i.e. **WORSE than just calling Claude directly.** And the turns that escalate are the
ambiguous / tool-heavy ones — the ones that decide the sale. So the cascade is fastest on the
cheap chat turns nobody worries about and slowest (worse than Claude-alone) on the turns that
matter. Backwards. Both also lose to realtime. This is what seals the park.

**Status: PARKED (2026-06-02, operator call).** Cascade shape confirmed = **SLM default + Claude
fallback** (NOT realtime+SLM; the cascade is a self-hosted *alternative* to realtime). Not wiring
it up: the probe answered the go/no-go, and the tool-turn latency makes the case for leaving
realtime weak. **Realtime stays LIVE.** Revive if vendor lock / per-min cost / voice-control
outweigh raw latency — then: wire Qwen into the local pipeline + heuristic router, re-probe with a
bigger tool corpus (5/6 on 6 cases isn't a trustworthy %), and a true coexistence run under
*active* whisper+kokoro inference (`REALTIME_BACKEND=local`, probe mid-call). Probe nit if reused:
free the prior model (gc + `torch.cuda.empty_cache()`) between dtype runs.

## Sources
- BFCL leaderboard: gorilla.cs.berkeley.edu/leaderboard.html ; pricepertoken.com/leaderboards/benchmark/bfcl-v3
- Practical 13-model local tool-calling eval (2026): jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm
- Docker "local LLM tool calling" practical eval: docker.com/blog/local-llm-tool-calling-a-practical-evaluation
