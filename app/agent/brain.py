"""Claude API streaming wrapper — the reasoning brain, now with tool use.

Takes the running conversation and yields text deltas as Claude generates them,
so downstream TTS can start on the first finished sentence. When Claude calls a
tool, this runs it (via the AriaTools dispatcher), feeds the result back, and keeps
streaming — all transparent to the caller, which still just consumes a text stream.
Conversation history is owned by the caller (the session).
"""
from __future__ import annotations

import time
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from config import settings

from app import trace
from app.agent.persona import system_prompt
from app.agent.tools import AriaTools
from app.log import debug
from app.runtime import engines

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

_MAX_TOOL_ROUNDS = 6  # safety bound on tool round-trips within one turn


def _input_preview(messages: list[dict]) -> str:
    """A short, readable description of what triggered this round — the last message
    sent to Claude (the caller's turn on round 0, or the tool results on later rounds)."""
    if not messages:
        return ""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "tool_result":
                parts.append(f"tool_result: {block.get('content')}")
            elif btype == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(str(btype))
        return " | ".join(p for p in parts if p)
    return str(content)


def stream_reply(history: list[dict], tools: AriaTools | None = None) -> AsyncIterator[str]:
    """Route the assistant's next turn to the configured brain (Phase 7).

    ``BRAIN_BACKEND=slm`` uses the local Qwen brain (``engines.slm``, GPU pod only);
    anything else uses Claude. Returns the chosen async iterator of spoken text deltas —
    both backends honour the same contract, so the pipeline is unchanged.
    """
    if settings.brain_backend == "slm":
        return engines.slm.stream_reply(history, tools)
    return _claude_stream_reply(history, tools)


async def _claude_stream_reply(history: list[dict], tools: AriaTools | None = None) -> AsyncIterator[str]:
    """Yield text deltas for the assistant's next turn given message history.

    ``history`` is a list of {"role", "content": str}. When ``tools`` is provided,
    Claude can call tools; tool turns are kept in a local working copy of the
    messages and are NOT written back to the session history (the spoken text is).
    Raises on API failure; the pipeline catches it and speaks ERROR_FALLBACK.
    """
    messages: list[dict] = list(history)
    tool_kwargs = {"tools": tools.schemas} if tools else {}
    # Cache the static prefix: tools come before system in Claude's cache order, so a
    # single cache_control breakpoint at the end of system caches tools + persona +
    # knowledge together. That's the longest stable prefix (history after it grows each
    # turn and stays uncached). NB: Haiku's cache minimum is 2048 tokens — if the
    # tools+system prefix is under that, caching silently no-ops (no error, no penalty).
    system_text = system_prompt() + (tools.call_context() if tools else "")
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    for round_idx in range(_MAX_TOOL_ROUNDS):
        t_req = time.perf_counter()
        ttft_ms = None
        chars = 0
        round_text = ""
        async with _client.messages.stream(
            model=settings.model,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            system=system,
            messages=messages,
            **tool_kwargs,
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    if ttft_ms is None:
                        # Pure Claude latency: request sent -> first text token.
                        # Round 0 is the perceived lead-in latency; later rounds
                        # are the post-tool answer (their prefill includes the
                        # tool result, so they read higher — that's expected).
                        ttft_ms = 1000 * (time.perf_counter() - t_req)
                        key = "claude_ttft_ms" if round_idx == 0 else f"claude_ttft_r{round_idx}_ms"
                        engines.last_metrics[key] = round(ttft_ms)
                        if round_idx == 0:
                            trace.stage("llm_ttft", ttft_ms)
                        debug(f"[metric] {key}={ttft_ms:.0f} (round {round_idx})")
                    chars += len(text)
                    round_text += text
                    yield text
            final = await stream.get_final_message()

        # Full streamed text per round, so we can read exactly what Claude said
        # (incl. any lead-in spoken before a tool call) without per-token noise.
        gen_ms = 1000 * (time.perf_counter() - t_req)
        usage = getattr(final, "usage", None)
        out_tok = getattr(usage, "output_tokens", None) if usage else None
        # cache_read > 0 proves the static prefix is being reused (the latency win);
        # cache_creation is the one-time write on the first turn of a call.
        cache_read = getattr(usage, "cache_read_input_tokens", None) if usage else None
        cache_write = getattr(usage, "cache_creation_input_tokens", None) if usage else None
        if round_idx == 0:
            engines.last_metrics["claude_cache_read_tok"] = cache_read or 0
            engines.last_metrics["claude_cache_write_tok"] = cache_write or 0
        # CAVEAT: gen_ms is wall-clock to the final message, and we consume the stream
        # LAZILY — the async-for that pulls tokens is suspended while Aria speaks each
        # sentence, so the SDK stream sits idle (back-pressure) between sentences. So
        # decode_ms/tok_s here are EFFECTIVE throughput as we drain it, NOT Claude's raw
        # decode rate (which is why they read 4-25 tok/s instead of Haiku's ~100+). The
        # only clean Claude latency number is ttft_ms; the only perceived-latency numbers
        # are ttft + chunk_buffer (llm_first_sentence - ttft), captured in the trace.
        decode_ms = (gen_ms - ttft_ms) if ttft_ms is not None else None
        tok_s = (round(out_tok / (decode_ms / 1000), 1)
                 if decode_ms and decode_ms > 0 and out_tok else None)
        if round_idx == 0:
            engines.last_metrics["claude_decode_tok_s"] = tok_s
        debug(
            f"[brain] round={round_idx} stop={final.stop_reason} "
            f"ttft_ms={round(ttft_ms) if ttft_ms is not None else 'n/a'} "
            f"decode_ms={round(decode_ms) if decode_ms is not None else 'n/a'} "
            f"tok_s={tok_s} gen_ms={gen_ms:.0f} chars={chars} out_tok={out_tok} "
            f"cache_read={cache_read} cache_write={cache_write} "
            f"text={round_text.strip()!r}"
        )
        trace.llm_round(
            round_idx=round_idx, input_preview=_input_preview(messages),
            ttft_ms=ttft_ms, gen_ms=gen_ms, decode_ms=decode_ms, tok_s=tok_s,
            stop=final.stop_reason, out_tok=out_tok,
            cache_read=cache_read, cache_write=cache_write, text=round_text,
        )

        if not tools or final.stop_reason != "tool_use":
            return

        # Run every tool Claude requested this round, feed results back, continue.
        tool_results = []
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            result = await tools.dispatch(block.name, block.input or {})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        messages.append({"role": "assistant", "content": final.content})
        messages.append({"role": "user", "content": tool_results})

    trace.error("max_tool_rounds", RuntimeError("hit max tool rounds; ending turn"))
