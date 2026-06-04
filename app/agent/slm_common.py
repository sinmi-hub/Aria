"""Shared SLM-path helpers for the cascade brain and the probe.

The Phase-7 probe (`scripts/slm_probe.py`) and the live SLM brain
(`app/agent/slm_brain.py`) must use the SAME prompt shaping and the SAME tool-call
parsing — otherwise the probe's 5/6 result wouldn't predict production. This module is
the single home for both, plus the SLM-path prompt fixes the probe discovered. Pure
Python (no torch import here), so it's unit-testable off-GPU.

The two SLM-path prompt fixes (kept OFF the Claude/realtime paths, which want the
opposite — a spoken lead-in masks their network tool latency):
  * Fix 1 — strip the persona's "LEAD-IN BEFORE YOU ACT" rule and tell the model to emit
    tool calls immediately as the whole turn (caller silence is covered by the micro-ack).
  * Fix 2 — sharpen book_meeting vs reschedule_meeting, which Qwen confused.
"""
from __future__ import annotations

import json
import re

LEADIN_MARKER = "LEAD-IN BEFORE YOU ACT:"
JOB_MARKER = "YOUR JOB on a call:"

TOOL_DIRECTIVE = (
    "\n\nTOOL CALLS: When you need a tool, emit the tool call IMMEDIATELY as your entire turn "
    "— do NOT write any spoken words before it, and do NOT announce that you're about to look "
    "someone up or check the calendar. Speak only on turns where you are NOT calling a tool."
)

SLM_TOOL_OVERRIDES = {
    "book_meeting": (
        "Book a NEW Settl demo for a lead who has NO demo booked yet, at one of the slots from "
        "find_open_slots. If the lead ALREADY has a demo and just wants a different time, use "
        "reschedule_meeting instead — NOT this. Requires a lead looked up first."
    ),
    "reschedule_meeting": (
        "Move the current lead's EXISTING booked demo to one of the slots from find_open_slots. "
        "Use this (NOT book_meeting) whenever the lead already has a demo and wants to change its "
        "time. Requires a lead with a booked meeting and a recent find_open_slots call."
    ),
}


def slm_system(system_text: str) -> str:
    """Strip the persona's lead-in-before-tools paragraph for the SLM path (Fix 1).

    Returns the persona WITHOUT the lead-in paragraph; callers append TOOL_DIRECTIVE last
    so it lands at highest recency. If the markers aren't found (persona reworded), the
    text is returned unchanged — the directive still steers behaviour.
    """
    start = system_text.find(LEADIN_MARKER)
    end = system_text.find(JOB_MARKER)
    if 0 <= start < end:
        system_text = system_text[:start] + system_text[end:]
    return system_text


def tool_specs(tuned: bool = True) -> list[dict]:
    """Aria's real Anthropic tool schemas in the OpenAI/chat-template `tools=` shape.

    With tuned=True (default) the book/reschedule descriptions are sharpened for the SLM
    (Fix 2); everything else is verbatim from production `SCHEMAS`.
    """
    from app.agent.tools import SCHEMAS

    specs = []
    for s in SCHEMAS:
        desc = SLM_TOOL_OVERRIDES[s["name"]] if (tuned and s["name"] in SLM_TOOL_OVERRIDES) \
            else s["description"]
        specs.append({
            "type": "function",
            "function": {"name": s["name"], "description": desc, "parameters": s["input_schema"]},
        })
    return specs


# --- tool-call parsing (Hermes/Qwen <tool_call>{json}</tool_call> + bare-JSON fallback) ---
_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from a model's raw output, returning [{name, arguments}].

    Handles the Hermes/Qwen `<tool_call>{json}</tool_call>` form first (what our
    candidates emit), then falls back to any bare JSON object that has a "name" plus
    "arguments"/"parameters" — so a model with a slightly different wrapper still works.
    """
    calls: list[dict] = []
    for blob in _TOOLCALL_RE.findall(text):
        parsed = _coerce_call(blob)
        if parsed:
            calls.append(parsed)
    if calls:
        return calls
    for blob in _json_objects(text):
        parsed = _coerce_call(blob)
        if parsed:
            calls.append(parsed)
    return calls


_TOOL_OPENER = "<tool_call>"


def classify_stream_prefix(text: str) -> str | None:
    """Early tool-vs-speech decision for the streaming brain, from a partial output prefix.

    Tool turns are emitted as `<tool_call>...` as the *entire* turn (the TOOL_DIRECTIVE),
    so we can decide before speaking any token:
      * first non-space char is not `<`           -> "speech" (flush immediately, no delay)
      * prefix starts `<tool_call>` / `<tool`     -> "tool"   (collect silently, never speak)
      * starts `<` but long enough to rule out a tool opener -> "speech"
      * starts `<` but still too short to tell     -> None     (wait for more tokens)
    `<tool_call>` is regular text in Qwen's Hermes template (not a special token), so it
    survives even with skip_special_tokens=True — which is what keeps spoken text clean.
    """
    s = text.lstrip()
    if not s:
        return None
    if s[0] != "<":
        return "speech"
    if s.startswith(_TOOL_OPENER) or s.startswith("<tool"):
        return "tool"
    if len(s) >= len(_TOOL_OPENER):
        return "speech"
    return None


def _coerce_call(blob: str) -> dict | None:
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}


def _json_objects(text: str) -> list[str]:
    """Yield top-level {...} substrings by brace-matching (cheap, good enough)."""
    out, depth, start = [], 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : i + 1])
    return out
