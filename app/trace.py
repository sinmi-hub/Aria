"""Structured, per-turn observability traces backing /debug/turns and /debug/errors.

Complements the free-text ring in ``app.log``: where that is a flat line buffer, this
is one structured record per turn — caller + Aria transcripts with wall-clock
timestamps, a per-stage latency breakdown (vad_tail -> stt -> llm_ttft -> chunk_buffer
-> tts -> ttfa), per-round LLM input/output traces, tool calls with timing, and
explicitly-tagged error events. All JSON-safe and readable over the proxy (no SSH).

Single active call per pod (one community GPU), so a module-global "current turn" is
safe here, mirroring ``engines.last_metrics``. If we ever run concurrent calls, this
moves onto the CallPipeline instance.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

from config import settings

_TURNS: deque[dict] = deque(maxlen=60)    # recent turn traces (newest last)
_ERRORS: deque[dict] = deque(maxlen=200)  # explicitly-tagged error events
_current: dict | None = None
_seq = 0

_PREVIEW = 240  # max chars stored for any text/result field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _clip(text) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= _PREVIEW else s[:_PREVIEW] + "…"


def start_turn(direction: str, caller_phone: str | None) -> None:
    """Open a new turn record. Everything after attaches to it until the next start."""
    global _current, _seq
    _seq += 1
    _current = {
        "turn": _seq,
        "ts": _now_iso(),
        "_t0": time.perf_counter(),  # stripped from the public view
        "direction": direction,
        "caller_phone": caller_phone,
        "caller_text": None,
        "aria_text": None,
        "stages_ms": {"vad_tail": settings.vad_silence_threshold_ms},
        "llm_rounds": [],
        "tools": [],
        "errors": [],
    }
    _TURNS.append(_current)


def set_caller(text: str) -> None:
    if _current is not None:
        _current["caller_text"] = _clip(text)


def set_aria(text: str) -> None:
    if _current is not None:
        _current["aria_text"] = _clip(text)


def mark_interrupted() -> None:
    """Flag the current turn as cut off by the caller before it finished speaking."""
    if _current is not None:
        _current["interrupted"] = True


def stage(name: str, ms: float) -> None:
    """Record a per-stage latency (ms). Names: stt, llm_ttft, llm_first_sentence,
    tts_first, ttfa. The derived breakdown is computed at read time."""
    if _current is not None:
        _current["stages_ms"][name] = round(ms)


def llm_round(*, round_idx: int, input_preview: str, ttft_ms, gen_ms: float,
              decode_ms=None, tok_s=None, stop: str, out_tok, cache_read, cache_write,
              text: str) -> None:
    """One Claude request/response within a turn (round 0 = the lead-in; later rounds
    are post-tool answers). ttft_ms is the clean Claude latency (request -> first token).
    decode_ms/tok_s are EFFECTIVE throughput as we drain the stream — contaminated by
    speak-backpressure (we stop pulling tokens while Aria talks), so they understate
    Claude's raw rate. Use ttft for latency, not tok_s."""
    if _current is not None:
        _current["llm_rounds"].append({
            "round": round_idx,
            "input": _clip(input_preview),
            "ttft_ms": round(ttft_ms) if ttft_ms is not None else None,
            "decode_ms": round(decode_ms) if decode_ms is not None else None,
            "tok_s": tok_s,
            "gen_ms": round(gen_ms),
            "stop": stop,
            "out_tok": out_tok,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": _clip(text),
        })


def tool_call(name: str, inp, result, ms: float) -> None:
    if _current is not None:
        _current["tools"].append({
            "name": name,
            "input": _clip(inp),
            "result": _clip(result),
            "ms": round(ms),
        })


def error(stage_name: str, exc) -> None:
    """Tag an explicit error event into BOTH the current turn and the global error ring,
    and mirror it to the flat log as a greppable ``[error:<stage>]`` line."""
    from app.log import warn
    etype = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
    rec = {
        "ts": _now_iso(),
        "stage": stage_name,
        "type": etype,
        "message": _clip(exc),
        "turn": _current["turn"] if _current is not None else None,
    }
    _ERRORS.append(rec)
    if _current is not None:
        _current["errors"].append(rec)
    warn(f"[error:{stage_name}] {etype}: {rec['message']}")


def recent_turns(n: int = 20) -> list[dict]:
    return [_public(t) for t in list(_TURNS)[-n:]]


def recent_errors(n: int = 50) -> list[dict]:
    return list(_ERRORS)[-n:]


def _public(t: dict) -> dict:
    """JSON-safe view: drop the internal perf_counter and add a derived, additive
    latency breakdown so the request waterfall is readable without arithmetic."""
    out = {k: v for k, v in t.items() if k != "_t0"}
    s = t["stages_ms"]
    breakdown = {"vad_tail": s.get("vad_tail")}
    if "stt" in s:
        breakdown["stt"] = s["stt"]
    if "llm_ttft" in s:
        breakdown["llm_ttft"] = s["llm_ttft"]
    # token -> first complete sentence (what sentence-chunking adds on top of TTFT)
    if "llm_first_sentence" in s and "llm_ttft" in s:
        breakdown["chunk_buffer"] = max(0, s["llm_first_sentence"] - s["llm_ttft"])
    if "tts_first" in s:
        breakdown["tts"] = s["tts_first"]
    if "ttfa" in s:
        breakdown["ttfa_total"] = s["ttfa"]
    out["breakdown_ms"] = breakdown
    return out
