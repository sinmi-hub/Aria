"""Off-GPU tests for the SLM brain's pure logic: stream classification + the speech/tool
separation in stream_reply (with a fake decoder, so no CUDA needed).

The model load + real decode are exercised only on the pod. What we CAN verify here is the
risky control flow: that tool-call tokens are never yielded as speech, that the spoken text
is exactly the model's spoken pieces, and that a tool turn dispatches then speaks pass 2.

    .venv/bin/python -m scripts.test_slm_brain
"""
from __future__ import annotations

import asyncio
import sys

from app.agent import slm_common
from app.agent.slm_brain import SlmBrain


def _check(name: str, cond: bool) -> bool:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    return cond


def test_classify() -> bool:
    ok = True
    c = slm_common.classify_stream_prefix
    ok &= _check("plain text -> speech", c("Sure") == "speech")
    ok &= _check("tool opener -> tool", c("<tool_call>") == "tool")
    ok &= _check("partial tool opener -> tool", c("<tool") == "tool")
    ok &= _check("leading ws + tool -> tool", c("\n  <tool_call>{") == "tool")
    ok &= _check("empty -> undecided", c("") is None)
    ok &= _check("lone '<' -> undecided", c("<") is None)
    ok &= _check("long non-tool '<' -> speech", c("<laughs warmly>") == "speech")
    return ok


class _FakeTok:
    """Stand-in tokenizer: stream_reply only needs apply_chat_template to produce *some*
    string (the real decode is faked by overriding _astream)."""

    def apply_chat_template(self, messages, **kw):
        return "PROMPT"


class _FakeTools:
    def __init__(self):
        self.dispatched = []

    def call_context(self):
        return "\n\nCALL CONTEXT: test."

    async def dispatch(self, name, args):
        self.dispatched.append((name, args))
        return f"result-for-{name}"


def _brain_with_outputs(rounds: list[str]) -> SlmBrain:
    """A SlmBrain whose _astream replays canned raw outputs, one per tool round."""
    brain = SlmBrain()
    brain.tok = _FakeTok()
    seq = iter(rounds)

    async def fake_astream(prompt):
        # Emit in small pieces to exercise the streaming classifier/buffer.
        text = next(seq)
        for i in range(0, len(text), 4):
            yield text[i : i + 4]

    brain._astream = fake_astream  # type: ignore[assignment]
    return brain


async def _collect(brain: SlmBrain, tools=None) -> str:
    out = []
    async for piece in brain.stream_reply([{"role": "user", "content": "hi"}], tools):
        out.append(piece)
    return "".join(out)


def test_speech_only() -> bool:
    brain = _brain_with_outputs(["It's $300 a month for founding companies."])
    spoken = asyncio.run(_collect(brain))
    return _check("speech turn yields full text",
                  spoken == "It's $300 a month for founding companies.")


def test_tool_then_speech() -> bool:
    ok = True
    tools = _FakeTools()
    brain = _brain_with_outputs([
        '<tool_call>\n{"name": "lookup_lead", "arguments": {"phone": "+15557654321"}}\n</tool_call>',
        "Hey Alex, good to hear from you again.",
    ])
    spoken = asyncio.run(_collect(brain, tools))
    ok &= _check("tool JSON never spoken", "<tool_call>" not in spoken and "lookup_lead" not in spoken)
    ok &= _check("pass-2 answer spoken", spoken == "Hey Alex, good to hear from you again.")
    ok &= _check("tool dispatched once", tools.dispatched == [("lookup_lead", {"phone": "+15557654321"})])
    return ok


def test_tool_without_tools_obj() -> bool:
    # If tools is None, a stray tool-call output must not crash; nothing is dispatched and
    # the turn just ends (no spoken text). Defensive: tools-less deploys still answer.
    brain = _brain_with_outputs(['<tool_call>{"name": "lookup_lead", "arguments": {}}</tool_call>'])
    spoken = asyncio.run(_collect(brain, None))
    return _check("tool output w/ no tools -> no speech, no crash", spoken == "")


def main() -> int:
    print("classify_stream_prefix:")
    a = test_classify()
    print("stream_reply speech:")
    b = test_speech_only()
    print("stream_reply tool->speech:")
    c = test_tool_then_speech()
    print("stream_reply tool w/o tools:")
    d = test_tool_without_tools_obj()
    ok = a and b and c and d
    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
