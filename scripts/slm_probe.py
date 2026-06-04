"""GPU-side probe for the Phase 7 cascade — does a local SLM (small model) earn its
place as Aria's default brain, with Claude as the escalation path?

This mirrors scripts/realtime_probe.py: a measurement-only spike, no pipeline wiring.
It loads ONE candidate SLM on the pod's GPU and measures the three things that decide
the cascade (per deploy/PHASE7-CASCADE-SLM-NOTES.md):

  1. TTFT (first-token) on the A4500 — is the ~100ms win over Claude-over-network real?
  2. Tool-calling reliability on OUR schemas — feed representative turns (lookup_lead by
     phone, find_open_slots, reschedule by number, a pricing chat turn, a backchannel,
     a do-not-call) and check it emits the right call (or correctly stays chat) using
     the model's OWN chat template + our real persona. This is the de-risk the notes
     warn about: "leaderboard rank != works-on-our-stack."
  3. VRAM coexistence — a whole-GPU snapshot (nvidia-smi, via app.gpu) before load,
     after load, and after a generation. Run this WHILE the uvicorn server is up
     (whisper + kokoro loaded) and the snapshot shows true headroom + contention.

WHY transformers + the model's native template (not vLLM/llama.cpp here): the native
chat template with `tools=` is the faithful upper-bound test of the model's tool-calling.
If it can't do tools even with its own template, it's hopeless; if it can, we then
separately validate whatever serving stack we'd actually deploy. This keeps the probe
about the MODEL, not the harness.

MODEL ID: --model defaults to a Qwen3 4B repo, the Phase 7 primary candidate. Verify the
exact HF repo id for the point release you want (the notes call it "Qwen3.5 4B"); pass
--model to override. If the repo id is wrong the probe reports the load error cleanly.

Run ON THE POD (needs the GPU). Get the pod's ssh host/port from runpod_up output, then:
    ssh -i ~/.ssh/voiceagent_rp -p <port> root@<ip> \
      'cd /app && python3 -m scripts.slm_probe --dtype 4bit'
Coexistence test: leave the server running (it holds whisper+kokoro) and run the probe
in a second ssh session — app.gpu's nvidia-smi reads the WHOLE GPU, so the snapshot
includes the server's VRAM.

Deps on the pod image: torch + transformers are baked. 4-bit needs `accelerate` +
`bitsandbytes` (pip3 install if missing); bf16 needs only `accelerate`. The probe prints
the exact install line if a dep is missing instead of crashing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time

# Prompt shaping + tool-call parsing are shared with the live SLM brain so the probe's
# result actually predicts production. parse_tool_calls is re-exported for test_slm_probe.
from app.agent.slm_common import (  # noqa: E402  (app import; resolved when run as a module)
    TOOL_DIRECTIVE,
    parse_tool_calls,
    slm_system,
    tool_specs,
)

DEFAULT_MODEL = "Qwen/Qwen3-4B"
MAX_NEW_TOKENS = 256
__all__ = ["parse_tool_calls", "score_case", "run"]


# The SLM-path prompt fixes (strip the lead-in rule, sharpen book/reschedule) and the
# tool schema conversion now live in app/agent/slm_common.py — shared with the live brain
# (slm_system, tool_specs, TOOL_DIRECTIVE imported above) so the probe tests production logic.


# --- representative turns (the de-risk corpus) -------------------------------
# Each case stacks the SAME system the pipeline builds (persona + call-context) plus a
# short prior-turn context, then asks: does the model emit the expected tool — or
# correctly stay chat? `expect` is a tool name, or None for "answer directly, no tool".
INBOUND_CTX = (
    "\n\nCALL CONTEXT: This is an inbound call. The caller's phone number is "
    "+15557654321. Call lookup_lead with this phone number right away to identify "
    "them — do not ask for their name first."
)


def _cases() -> list[dict]:
    return [
        {
            "name": "lookup_by_phone",
            "system_extra": INBOUND_CTX,
            "messages": [
                {"role": "assistant", "content": "Thanks for calling Settl, this is Aria — how can I help?"},
                {"role": "user", "content": "Yeah hi, I called earlier about the moving software."},
            ],
            "expect": "lookup_lead",
            "expect_args": {"phone": lambda v: bool(v)},
        },
        {
            "name": "pricing_chat_no_tool",
            "system_extra": INBOUND_CTX,
            "messages": [
                {"role": "assistant", "content": "Good to talk to you, Sarah — what can I help with?"},
                {"role": "user", "content": "How much does Settl cost?"},
            ],
            "expect": None,  # persona: don't look anyone up just to answer pricing
        },
        {
            "name": "find_slots",
            "system_extra": INBOUND_CTX,
            "messages": [
                {"role": "assistant", "content": "Great to hear from you, Sarah at Acme Movers!"},
                {"role": "user", "content": "Can we set up a demo sometime next week?"},
            ],
            "expect": "find_open_slots",
        },
        {
            "name": "reschedule_by_number",
            "system_extra": INBOUND_CTX,
            "messages": [
                {"role": "assistant", "content": "You're booked for Tuesday at two. I can move it — I've got Wednesday at ten, or Thursday at three."},
                {"role": "user", "content": "Let's do the second one."},
            ],
            "expect": "reschedule_meeting",
            "expect_args": {"slot_number": 2},
        },
        {
            "name": "backchannel_no_tool",
            "system_extra": INBOUND_CTX,
            "messages": [
                {"role": "assistant", "content": "Settl handles your quoting, scheduling, dispatch, and payments in one place."},
                {"role": "user", "content": "Mm-hmm."},
            ],
            "expect": None,  # a brief continuation, not a tool
        },
        {
            "name": "do_not_call",
            "system_extra": INBOUND_CTX,
            "messages": [
                {"role": "assistant", "content": "Good to talk to you — what can I help with?"},
                {"role": "user", "content": "Honestly, just take me off your list. Don't call again."},
            ],
            "expect": "mark_do_not_call",
        },
    ]


def score_case(case: dict, calls: list[dict]) -> tuple[bool, str]:
    """Did the model do the right thing for this turn? Returns (passed, reason)."""
    expect = case["expect"]
    first = calls[0] if calls else None
    if expect is None:
        if first is None:
            return True, "stayed chat (correct)"
        return False, f"called {first['name']} but should have answered directly"
    if first is None:
        return False, f"no tool call; expected {expect}"
    if first["name"] != expect:
        return False, f"called {first['name']}; expected {expect}"
    for key, want in case.get("expect_args", {}).items():
        got = first["arguments"].get(key)
        ok = want(got) if callable(want) else (got == want)
        if not ok:
            return False, f"{expect} arg {key}={got!r} did not match expectation"
    return True, f"called {expect} correctly"


# --- model + generation -----------------------------------------------------
class ProbeError(RuntimeError):
    """A clean, user-facing failure (missing dep, no CUDA, bad repo id). The CLI prints
    it and exits 1; the /debug endpoint stores it as the run's error — neither crashes."""


def _load(model_id: str, dtype: str, emit=print):
    """Load tokenizer + model on CUDA. Returns (tok, model) or raises ProbeError with an
    actionable message if a dep/model is missing."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise ProbeError(f"missing dep: {exc}. Pod needs: pip3 install 'transformers>=4.51,<5' accelerate")

    if not torch.cuda.is_available():
        raise ProbeError("no CUDA — run this ON THE POD (the dev box has no GPU).")

    quant = None
    if dtype == "4bit":
        try:
            from transformers import BitsAndBytesConfig
            import bitsandbytes  # noqa: F401  (presence check)

            quant = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
            )
        except ImportError:
            raise ProbeError("--dtype 4bit needs bitsandbytes: pip3 install bitsandbytes accelerate")

    emit(f"[slm] loading {model_id} (dtype={dtype}) ...")
    try:
        tok = AutoTokenizer.from_pretrained(model_id)
        kwargs = {"device_map": "cuda"}
        if quant is not None:
            kwargs["quantization_config"] = quant
        else:
            kwargs["torch_dtype"] = torch.bfloat16
        model = AutoModelForCausalLM.from_pretrained(model_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise ProbeError(f"load failed for {model_id!r}: {type(exc).__name__}: {exc} "
                         f"(check the HF repo id and that transformers is new enough)")
    return tok, model


def _build_prompt(tok, case: dict, tool_specs: list[dict], tuned: bool = True) -> str:
    """Apply the model's own chat template with our system + tools. enable_thinking is
    forced OFF when supported — thinking-mode prefill would wreck TTFT and isn't what a
    fast phone turn wants. With tuned=True the SLM-path prompt fixes are applied (Fix 1)."""
    from app.agent.persona import system_prompt

    base = slm_system(system_prompt()) if tuned else system_prompt()
    system = base + case["system_extra"] + (TOOL_DIRECTIVE if tuned else "")
    messages = [{"role": "system", "content": system}, *case["messages"]]
    kw = dict(tools=tool_specs, add_generation_prompt=True, tokenize=False)
    try:
        return tok.apply_chat_template(messages, enable_thinking=False, **kw)
    except TypeError:
        return tok.apply_chat_template(messages, **kw)  # template lacks the kwarg


def _generate(tok, model, prompt: str) -> tuple[str, float]:
    """Greedy generation with first-token timing. Returns (full_text, ttft_ms)."""
    import torch
    from transformers import TextIteratorStreamer

    inputs = tok(prompt, return_tensors="pt").to(model.device)
    streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=False)
    gen_kwargs = dict(
        **inputs,
        max_new_tokens=MAX_NEW_TOKENS,
        do_sample=False,
        streamer=streamer,
        pad_token_id=tok.eos_token_id,
    )
    t0 = time.perf_counter()
    thread = threading.Thread(target=model.generate, kwargs=gen_kwargs)
    thread.start()
    first_ms = None
    chunks: list[str] = []
    for piece in streamer:
        if first_ms is None and piece.strip():
            first_ms = 1000 * (time.perf_counter() - t0)
        chunks.append(piece)
    thread.join()
    with torch.no_grad():
        torch.cuda.synchronize()
    return "".join(chunks), (first_ms or 0.0)


def _gpu() -> dict:
    from app import gpu

    return gpu.snapshot()


def _fmt_gpu(snap: dict) -> str:
    if snap.get("source", "").startswith("nvidia"):
        return (f"{snap['name']}: {snap['vram_used_mb']}/{snap['vram_total_mb']}MB "
                f"({snap['vram_used_pct']}%), util {snap['gpu_util_pct']}%")
    return json.dumps(snap)


def run(model_id: str, dtype: str, warmup: bool, emit=print, tuned: bool = True) -> dict:
    """Run the probe and RETURN a structured result dict (so the /debug/slm_probe
    endpoint can serve it as JSON), while also emitting human lines via `emit`
    (stdout on the CLI, the in-memory log ring when driven over HTTP). tuned=True applies
    the Phase-7 SLM-path prompt fixes (drop lead-in rule + sharpen book/reschedule)."""
    gpu_before = _gpu()
    emit(f"[slm] === SLM probe: {model_id} (prompt={'tuned' if tuned else 'raw'}) ===")
    emit(f"[slm] gpu before load: {_fmt_gpu(gpu_before)}")

    tok, model = _load(model_id, dtype, emit)
    after_load = _gpu()
    emit(f"[slm] gpu after load:  {_fmt_gpu(after_load)}")

    specs = tool_specs(tuned)
    cases = _cases()

    if warmup:
        # First generate pays CUDA-graph/compile warmup — exclude it from TTFT stats.
        _generate(tok, model, _build_prompt(tok, cases[0], specs, tuned))
        emit("[slm] warmup generation done (excluded from stats)")

    rows = []
    for case in cases:
        prompt = _build_prompt(tok, case, specs, tuned)
        text, ttft_ms = _generate(tok, model, prompt)
        calls = parse_tool_calls(text)
        passed, reason = score_case(case, calls)
        rows.append({"name": case["name"], "ttft_ms": round(ttft_ms),
                     "passed": passed, "reason": reason,
                     "calls": [c["name"] for c in calls],
                     "raw": text.strip()[:300] if not passed else None})
        mark = "OK " if passed else "FAIL"
        emit(f"  [{mark}] {case['name']:22s} ttft={round(ttft_ms):4d}ms  {reason}")
        if not passed:
            emit(f"         raw: {text.strip()[:200]!r}")

    after_gen = _gpu()
    emit(f"[slm] gpu after generations: {_fmt_gpu(after_gen)}")

    ttfts = sorted(r["ttft_ms"] for r in rows if r["ttft_ms"])
    n_pass = sum(1 for r in rows if r["passed"])
    p95 = (ttfts[min(len(ttfts) - 1, int(round(0.95 * (len(ttfts) - 1))))] if ttfts else None)
    summary = {
        "model": model_id,
        "dtype": dtype,
        "prompt": "tuned" if tuned else "raw",
        "tool_calling": {"pass": n_pass, "total": len(rows)},
        "ttft_ms": ({"min": ttfts[0], "median": round(statistics.median(ttfts)),
                     "p95": p95, "max": ttfts[-1], "n": len(ttfts)} if ttfts else None),
        "gpu": {"before_load": gpu_before, "after_load": after_load, "after_gen": after_gen},
        "cases": rows,
    }
    emit("=== summary ===")
    emit(f"  tool-calling: {n_pass}/{len(rows)} correct")
    if ttfts:
        emit(f"  TTFT(first-token): min={ttfts[0]}  median={round(statistics.median(ttfts))}  "
             f"p95={p95}  max={ttfts[-1]}  n={len(ttfts)}")
    emit("=== vs the alternatives ===")
    emit("  Claude (network) TTFT ~600ms typical; OpenAI realtime first-audio ~333ms.")
    emit("  Cascade goal: SLM TTFT here + ~130 STT + ~160 chunk + ~124 TTS -> target < 1s TTFA.")
    return summary


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="slm_probe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=DEFAULT_MODEL, help="HF repo id of the candidate SLM")
    p.add_argument("--dtype", choices=["4bit", "bf16"], default="4bit",
                   help="4bit (matches the ~3.4GB production footprint) or bf16")
    p.add_argument("--no-warmup", action="store_true", help="include the first (warmup) gen in stats")
    p.add_argument("--raw-prompt", action="store_true",
                   help="use the production persona verbatim (skip the Phase-7 SLM prompt fixes)")
    args = p.parse_args(argv)
    try:
        run(args.model, args.dtype, warmup=not args.no_warmup, tuned=not args.raw_prompt)
    except ProbeError as exc:
        sys.exit(f"[slm] {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
