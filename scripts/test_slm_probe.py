"""Local tests for the SLM probe's pure logic — tool-call parsing + case scoring.

These are the GPU-free parts: if the parser misreads a model's output or the scorer
mis-grades a turn, the probe's headline numbers are wrong. Run on the dev box:
    .venv/bin/python -m scripts.test_slm_probe
(The GPU load/generate path is exercised only on the pod via `python -m scripts.slm_probe`.)
"""
from __future__ import annotations

import sys

from scripts.slm_probe import parse_tool_calls, score_case


def _check(name: str, cond: bool) -> bool:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    return cond


def test_parse() -> bool:
    ok = True
    # Hermes/Qwen wrapper, the common case.
    c = parse_tool_calls('<tool_call>\n{"name": "lookup_lead", "arguments": {"phone": "+15557654321"}}\n</tool_call>')
    ok &= _check("hermes wrapper -> 1 call", len(c) == 1 and c[0]["name"] == "lookup_lead")
    ok &= _check("hermes args parsed", c and c[0]["arguments"].get("phone") == "+15557654321")

    # Lead-in text before the call (Aria's spoken line) must not break parsing.
    c = parse_tool_calls('Sure, one sec. <tool_call>{"name": "find_open_slots", "arguments": {}}</tool_call>')
    ok &= _check("text + wrapper", len(c) == 1 and c[0]["name"] == "find_open_slots")

    # Bare JSON fallback (no wrapper), arguments-as-string variant.
    c = parse_tool_calls('{"name": "mark_do_not_call", "arguments": "{\\"reason\\": \\"asked\\"}"}')
    ok &= _check("bare json + stringified args", len(c) == 1 and c[0]["arguments"].get("reason") == "asked")

    # Plain chat (no tool) -> no calls.
    ok &= _check("plain text -> 0 calls", parse_tool_calls("It's $300 a month for founding companies.") == [])

    # "parameters" key instead of "arguments".
    c = parse_tool_calls('<tool_call>{"name": "reschedule_meeting", "parameters": {"slot_number": 2}}</tool_call>')
    ok &= _check("parameters key", c and c[0]["arguments"].get("slot_number") == 2)
    return ok


def test_score() -> bool:
    ok = True
    expect_tool = {"name": "x", "expect": "lookup_lead", "expect_args": {"phone": lambda v: bool(v)}}
    passed, _ = score_case(expect_tool, [{"name": "lookup_lead", "arguments": {"phone": "+1"}}])
    ok &= _check("right tool + arg -> pass", passed)

    passed, _ = score_case(expect_tool, [{"name": "lookup_lead", "arguments": {}}])
    ok &= _check("right tool, bad arg -> fail", not passed)

    passed, _ = score_case(expect_tool, [])
    ok &= _check("expected tool, none called -> fail", not passed)

    chat = {"name": "x", "expect": None}
    ok &= _check("chat expected, no call -> pass", score_case(chat, [])[0])
    ok &= _check("chat expected, tool called -> fail",
                 not score_case(chat, [{"name": "lookup_lead", "arguments": {}}])[0])

    num = {"name": "x", "expect": "reschedule_meeting", "expect_args": {"slot_number": 2}}
    ok &= _check("exact-value arg match", score_case(num, [{"name": "reschedule_meeting", "arguments": {"slot_number": 2}}])[0])
    ok &= _check("exact-value arg mismatch -> fail",
                 not score_case(num, [{"name": "reschedule_meeting", "arguments": {"slot_number": 1}}])[0])
    return ok


def main() -> int:
    print("parse_tool_calls:")
    p = test_parse()
    print("score_case:")
    s = test_score()
    ok = p and s
    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
