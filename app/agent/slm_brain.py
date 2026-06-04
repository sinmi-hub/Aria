"""Local SLM brain for the cascade — Qwen on the pod's GPU, drop-in for the Claude brain.

Selected for the LOCAL pipeline when ``BRAIN_BACKEND=slm`` (Phase 7). Mirrors
``brain.stream_reply``'s contract exactly: ``stream_reply(history, tools)`` is an async
iterator of SPOKEN text deltas, so the pipeline's sentence chunker + TTS are unchanged.
Tool calls are run transparently (the two-pass local pattern) and never spoken.

SLM-only for now: there is NO Claude escalation here — we want the pure SLM latency floor
first. Escalation/routing is a later phase (see PHASE7-CASCADE-IMPLEMENTATION.md).

Two-pass tool flow (a local model can't stream a tool call into speech):
  pass 1 — model emits ``<tool_call>{...}</tool_call>`` as the whole turn (TOOL_DIRECTIVE);
           we collect it silently, parse, and dispatch via AriaTools.
  pass 2 — feed the tool result back; the model now speaks the answer, which we stream.

KV-prewarm (``SLM_KV_PREWARM``, lever #2): the system+tools prefix is identical every turn,
so we prefill it once per call and reuse its ``past_key_values`` — per-turn prefill becomes
just the new conversation tokens. Opt-out with a clean fallback to full prefill: it's
transformers-version-sensitive and can't be validated off-GPU.

GPU-only: ``load()`` needs CUDA + transformers (baked into the pod image). The pure logic
(prompt assembly, stream classification, tool parsing) lives in ``slm_common`` and is
unit-tested off-GPU in ``scripts/test_slm_brain.py``.
"""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from typing import AsyncIterator

from config import settings

from app import trace
from app.agent import slm_common
from app.agent.persona import system_prompt
from app.agent.tools import AriaTools
from app.log import debug, warn
from app.runtime import engines

_MAX_TOOL_ROUNDS = 4          # local tool round-trips per turn (matches the call's needs)
_CLASSIFY_BUDGET = 24         # chars to buffer before forcing a speech/tool decision
_STREAM_SENTINEL = object()   # marks the blocking streamer's end across the executor bridge


def _next_blocking(streamer) -> object:
    """Pull one piece from a TextIteratorStreamer, returning the sentinel at end-of-stream
    (run in the default executor so the event loop stays free during decode)."""
    try:
        return next(streamer)
    except StopIteration:
        return _STREAM_SENTINEL


class SlmBrain:
    """Process-wide singleton holding the loaded model + the prewarmed prefix cache."""

    def __init__(self) -> None:
        self.tok = None
        self.model = None
        self._prefix_ids = None        # token ids of the static system+tools prefix
        self._prefix_cache = None      # past_key_values for that prefix (prewarm)
        self._prewarm_failed = False   # set once if reuse errors -> permanent fallback
        self._specs = slm_common.tool_specs(tuned=True)

    # --- load + prewarm (call once at startup, ON THE POD) --------------------
    def load(self) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("SlmBrain needs CUDA — BRAIN_BACKEND=slm only runs on the pod.")

        model_id, dtype = settings.slm_model, settings.slm_dtype
        print(f"[slm-brain] loading {model_id} (dtype={dtype}) ...")
        self.tok = AutoTokenizer.from_pretrained(model_id)
        kwargs: dict = {"device_map": "cuda"}
        if dtype == "4bit":
            from transformers import BitsAndBytesConfig

            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16)
        else:
            kwargs["torch_dtype"] = torch.bfloat16
        self.model = AutoModelForCausalLM.from_pretrained(model_id, **kwargs)
        self.model.eval()
        self._prewarm()
        print("[slm-brain] ready")

    def _static_system(self, tools: AriaTools | None) -> str:
        """The persona with the SLM-path fixes + the tool directive. The call-context block
        (caller phone etc.) is appended PER TURN by the caller — keep it OUT of the prefix so
        the prewarmed prefix stays identical across calls."""
        return slm_common.slm_system(system_prompt()) + slm_common.TOOL_DIRECTIVE

    def _prewarm(self) -> None:
        """Prefill the static system+tools prefix once and cache its past_key_values.

        Built without any call-context or conversation, so it's the longest stable prefix.
        On any failure we disable prewarm (full prefill still produces correct output).
        """
        if not settings.slm_kv_prewarm:
            return
        try:
            import torch

            prefix_text = self._render_prefix()
            self._prefix_ids = self.tok(prefix_text, return_tensors="pt").input_ids.to(self.model.device)
            with torch.no_grad():
                out = self.model(self._prefix_ids, use_cache=True)
            self._prefix_cache = out.past_key_values
            debug(f"[slm-brain] prewarmed prefix ({self._prefix_ids.shape[1]} tokens)")
        except Exception as exc:  # noqa: BLE001 - prewarm is an optimization, never fatal
            self._prewarm_failed = True
            warn(f"[slm-brain] prewarm failed, falling back to full prefill: {exc!r}")

    def _render_prefix(self) -> str:
        """Templated text of just the system block — a true token-prefix of every turn's
        prompt (Qwen concatenates per-message), so its KV is reusable."""
        messages = [{"role": "system", "content": self._static_system(None)}]
        return self.tok.apply_chat_template(
            messages, tools=self._specs, add_generation_prompt=False, tokenize=False)

    # --- prompt build ---------------------------------------------------------
    def _build_prompt(self, history: list[dict], tools: AriaTools | None) -> str:
        system = self._static_system(tools) + (tools.call_context() if tools else "")
        messages = [{"role": "system", "content": system}, *history]
        return self._template(messages)

    def _template(self, messages: list[dict]) -> str:
        kw = dict(tools=self._specs, add_generation_prompt=True, tokenize=False)
        try:
            return self.tok.apply_chat_template(messages, enable_thinking=False, **kw)
        except TypeError:
            return self.tok.apply_chat_template(messages, **kw)

    # --- generation (async bridge over the blocking streamer) -----------------
    async def _astream(self, prompt: str) -> AsyncIterator[str]:
        """Yield raw text pieces as the model decodes. Uses the prewarmed prefix cache when
        the current prompt actually starts with it; otherwise full prefill. Reports TTFT."""
        import torch
        from transformers import TextIteratorStreamer

        input_ids = self.tok(prompt, return_tensors="pt").input_ids.to(self.model.device)
        streamer = TextIteratorStreamer(self.tok, skip_prompt=True, skip_special_tokens=True)
        gen_kwargs: dict = dict(
            input_ids=input_ids,
            max_new_tokens=settings.slm_max_new_tokens,
            do_sample=False,
            streamer=streamer,
            pad_token_id=self.tok.eos_token_id,
        )
        self._maybe_attach_prefix_cache(gen_kwargs, input_ids)

        t0 = time.perf_counter()
        thread = threading.Thread(target=self.model.generate, kwargs=gen_kwargs)
        thread.start()
        loop = asyncio.get_event_loop()
        first = True
        try:
            while True:
                piece = await loop.run_in_executor(None, _next_blocking, streamer)
                if piece is _STREAM_SENTINEL:
                    break
                if first and piece.strip():
                    ttft = 1000 * (time.perf_counter() - t0)
                    engines.last_metrics.setdefault("slm_ttft_ms", round(ttft))
                    trace.stage("llm_ttft", ttft)
                    debug(f"[slm-brain] ttft={ttft:.0f}ms")
                    first = False
                yield piece
        finally:
            await loop.run_in_executor(None, thread.join)
            with torch.no_grad():
                torch.cuda.synchronize()

    def _maybe_attach_prefix_cache(self, gen_kwargs: dict, input_ids) -> None:
        """If the prewarmed prefix is a real prefix of this prompt, hand generate a fresh
        copy of its cache so it only prefills the new tokens. Deep-copied because generate
        consumes/extends the cache. Any mismatch -> skip (full prefill, still correct)."""
        if self._prefix_cache is None or self._prewarm_failed or self._prefix_ids is None:
            return
        plen = self._prefix_ids.shape[1]
        if input_ids.shape[1] <= plen:
            return
        import torch

        if not torch.equal(input_ids[:, :plen], self._prefix_ids):
            return  # template drift — don't risk a corrupt cache
        try:
            gen_kwargs["past_key_values"] = copy.deepcopy(self._prefix_cache)
            gen_kwargs["attention_mask"] = torch.ones_like(input_ids)
        except Exception as exc:  # noqa: BLE001
            self._prewarm_failed = True
            warn(f"[slm-brain] cache reuse failed, disabling prewarm: {exc!r}")

    # --- the public contract: mirror brain.stream_reply -----------------------
    async def stream_reply(self, history: list[dict],
                           tools: AriaTools | None = None) -> AsyncIterator[str]:
        """Yield spoken text deltas for the assistant's next turn. Tool calls run silently
        (two-pass) and are never yielded. Working messages (tool turns) stay local — only
        the spoken text is the caller's to persist (same as the Claude brain)."""
        messages: list[dict] = [
            {"role": "system", "content": self._static_system(tools) + (tools.call_context() if tools else "")},
            *history,
        ]
        for round_idx in range(_MAX_TOOL_ROUNDS):
            prompt = self._template(messages)
            raw, decided = "", None
            buffer = ""
            async for piece in self._astream(prompt):
                raw += piece
                if decided == "speech":
                    yield piece
                    continue
                if decided == "tool":
                    continue
                buffer += piece
                decided = slm_common.classify_stream_prefix(buffer)
                if decided is None and len(buffer) < _CLASSIFY_BUDGET:
                    continue
                if decided != "tool":          # speech (or undecided past budget -> treat as speech)
                    decided = "speech"
                    if buffer:
                        yield buffer
                    buffer = ""

            calls = slm_common.parse_tool_calls(raw) if (decided == "tool" or "<tool_call>" in raw) else []
            if not (tools and calls):
                debug(f"[slm-brain] round={round_idx} spoke: {raw.strip()[:120]!r}")
                return

            # Two-pass: run the tool(s), feed results back, loop for the spoken answer.
            messages.append(self._assistant_toolcall_msg(calls))
            for call in calls:
                args = call.get("arguments") or {}
                t_tool = time.perf_counter()
                result = await tools.dispatch(call["name"], args)
                tool_ms = 1000 * (time.perf_counter() - t_tool)
                messages.append({"role": "tool", "content": str(result)})
                trace.tool_call(call["name"], args, str(result), tool_ms)
                debug(f"[slm-brain] round={round_idx} tool {call['name']}({args}) {tool_ms:.0f}ms")

        trace.error("slm_max_tool_rounds", RuntimeError("hit max SLM tool rounds; ending turn"))

    @staticmethod
    def _assistant_toolcall_msg(calls: list[dict]) -> dict:
        """The assistant turn that carries the tool call(s), in the chat-template shape."""
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function",
                 "function": {"name": c["name"], "arguments": c.get("arguments") or {}}}
                for c in calls
            ],
        }
