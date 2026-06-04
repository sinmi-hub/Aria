"""Buffers a token stream into complete sentences.

The whole latency trick: we fire TTS on the first finished sentence instead of
waiting for Claude to complete. This wraps an async token iterator and yields
sentence-sized strings as soon as a boundary (. ? !) is seen, flushing whatever
remains at the end.
"""
from __future__ import annotations

from typing import AsyncIterator

_BOUNDARIES = ".?!"
# Guard against emitting tiny "." fragments ("Hi.", "Mr.", "9 a.m.") that hurt TTS
# prosody. Tuned to 7 so a leading reaction "Got it." (7 chars +".") DOES flush at
# time-to-first-token — Call 3 measured 779ms lost when an 8-char guard held it past
# the next clause. "Got you." (8) flushes too; "Mr." (3) / "Hi." (3) / "9 a.m." (4)
# stay guarded. The guard is `idx + 1 < _MIN_CHARS`, so 7 frees fragments of length 7+.
_MIN_CHARS = 7


async def sentences(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    buf = ""
    async for tok in tokens:
        buf += tok
        # Emit on a sentence boundary. '!' and '?' are unambiguous sentence ends, so
        # we flush them immediately even when short — that lets a quick leading
        # reaction ("Perfect!", "Got it?") reach TTS at time-to-first-token instead
        # of waiting for a full clause, which is the dominant slice of the turn pause.
        # '.' keeps the min-length guard (avoids splitting "9 a.m." / "Mr." fragments).
        while True:
            idx = _boundary_index(buf)
            if idx == -1:
                break
            if buf[idx] == "." and idx + 1 < _MIN_CHARS:
                break
            sentence, buf = buf[: idx + 1], buf[idx + 1 :].lstrip()
            text = sentence.strip()
            if text:
                yield text
    tail = buf.strip()
    if tail:
        yield tail


def _boundary_index(buf: str) -> int:
    """Index of the earliest sentence-ending punctuation, or -1."""
    best = -1
    for ch in _BOUNDARIES:
        i = buf.find(ch)
        if i != -1 and (best == -1 or i < best):
            best = i
    return best
