"""Overlapped STT for the cascade (Phase 7 lever #1).

Today STT runs on the whole utterance AFTER end-of-speech — a sequential ~130ms tax on
every turn. This transcribes the utterance *while the caller is still talking*: the
segmenter emits ``speech_partial`` snapshots during speech, we transcribe the latest one
in a background thread, and at end-of-speech the transcript is usually already done —
so the post-VAD STT cost collapses toward zero.

Correctness is preserved by a coverage check: a streamed partial is only reused if it
covered enough of the final utterance's samples; otherwise the worker does a normal full
transcribe (the trailing VAD-silence tail carries no words, so a partial taken at the last
speech window typically covers ~all the spoken content). This never changes VAD turn
boundaries or barge-in — it only moves *when* the transcription happens.
"""
from __future__ import annotations

import asyncio

import numpy as np


class StreamingTranscriber:
    """Per-call overlapped transcriber. Driven by the pipeline's recv loop (single-threaded
    asyncio), so the partial-result fields are updated cooperatively — no locking needed."""

    def __init__(self, stt, coverage: float = 0.85) -> None:
        self.stt = stt
        self.coverage = coverage
        self._text = ""
        self._samples = 0          # samples covered by the latest completed partial
        self._busy = False         # one partial transcription in flight at a time

    def feed(self, partial: np.ndarray) -> None:
        """Schedule a background transcription of the speech-so-far, if idle. Drops the
        request when a prior partial is still decoding (the next snapshot supersedes it)."""
        if self._busy or partial.size == 0:
            return
        self._busy = True
        asyncio.create_task(self._run(partial))

    async def _run(self, partial: np.ndarray) -> None:
        try:
            text = (await asyncio.to_thread(self.stt.transcribe, partial)).strip()
            if partial.size >= self._samples:   # keep the longest-covered partial
                self._text, self._samples = text, partial.size
        finally:
            self._busy = False

    def take_if_covers(self, full: np.ndarray) -> str | None:
        """Return the streamed transcript if it covered enough of this utterance, else None
        (caller falls back to a full transcribe). Read synchronously at end-of-speech."""
        if full.size and self._text and self._samples >= self.coverage * full.size:
            return self._text
        return None

    def reset(self) -> None:
        self._text = ""
        self._samples = 0
