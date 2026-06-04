"""Offline test of the turn-taking decision — no audio devices, no real models.

This is the test whose absence let gpu-phase5e ship the audio-starvation regression:
cancelling a turn during silent think-time starved the caller of audio. It drives
``CallPipeline._classify_and_act`` directly with fake engines + a fake session and
asserts the cancel/ignore/defer decision for each case.

Run:  .venv/bin/python -m scripts.test_turn_taking
"""
from __future__ import annotations

import asyncio
import sys

import numpy as np

from config import settings
from app import runtime
from app.agent import pipeline as pl


class _FakeSTT:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def transcribe(self, arr) -> str:
        return self.text


class _FakeSession:
    call_control_id = "test"

    def add_user(self, *_): pass
    def add_assistant(self, *_): pass
    async def send_media(self, *_): pass
    async def send_clear(self): pass


def _arr(ms: int) -> np.ndarray:
    return np.zeros(int(16000 * ms / 1000), dtype=np.float32)


def _make_pipeline(stt_text: str = "") -> pl.CallPipeline:
    runtime.engines.vad = object()
    runtime.engines.stt = _FakeSTT(stt_text)
    runtime.engines.tts = object()
    # Bypass the segmenter (needs a real VAD) and the worker loop — we call the
    # classifier directly.
    p = pl.CallPipeline.__new__(pl.CallPipeline)
    p.session = _FakeSession()
    p.utterances = asyncio.Queue()  # a stray debounce timer may commit here on teardown
    p.speaking = False
    p._audio_active = False
    p._first_audio_sent = False
    p._turn_started = 0.0
    p._barge_ins = 0
    p._silent_barge_ins = 0
    p._pending = []
    p._debounce_handle = None
    p.turn_task = None
    p._closed = False
    return p


async def _fake_turn(p):
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        p._turn_cancelled = True
        raise


def _check(name: str, cond: bool) -> bool:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    return cond


async def main() -> None:
    ok = True

    # Case 1: real interruption WHILE SPEAKING (audio_active) -> cancel the turn.
    p = _make_pipeline()
    p.speaking = True
    p._audio_active = True
    p._turn_cancelled = False
    p.turn_task = asyncio.create_task(_fake_turn(p))
    await asyncio.sleep(0)
    await p._classify_and_act(_arr(2000))  # >hard_interrupt, no STT needed
    await asyncio.sleep(0)
    ok &= _check("interruption over audio -> turn cancelled", p._turn_cancelled)
    ok &= _check("interruption over audio -> buffered", len(p._pending) == 1)

    # Case 2: real interruption DURING THINK (no audio yet) -> do NOT cancel.
    p = _make_pipeline()
    p.speaking = True            # generating
    p._audio_active = False      # nothing spoken yet
    p._turn_cancelled = False
    p.turn_task = asyncio.create_task(_fake_turn(p))
    await asyncio.sleep(0)
    await p._classify_and_act(_arr(2000))
    await asyncio.sleep(0)
    ok &= _check("interruption during think -> turn NOT cancelled (no starvation)",
                 not p._turn_cancelled)
    ok &= _check("interruption during think -> buffered as follow-up", len(p._pending) == 1)
    p.turn_task.cancel()

    # Case 3: backchannel while speaking -> ignored (no cancel, not buffered).
    p = _make_pipeline(stt_text="okay")
    p.speaking = True
    p._audio_active = True
    p._turn_cancelled = False
    p.turn_task = asyncio.create_task(_fake_turn(p))
    await asyncio.sleep(0)
    await p._classify_and_act(_arr(700))  # in blip..hard band -> transcribed -> "okay"
    await asyncio.sleep(0)
    ok &= _check("backchannel 'okay' over audio -> NOT cancelled", not p._turn_cancelled)
    ok &= _check("backchannel 'okay' -> NOT buffered", len(p._pending) == 0)
    p.turn_task.cancel()

    # Case 4: blip (too short) while speaking -> ignored.
    p = _make_pipeline(stt_text="anything")
    p.speaking = True
    p._audio_active = True
    p._turn_cancelled = False
    p.turn_task = asyncio.create_task(_fake_turn(p))
    await asyncio.sleep(0)
    await p._classify_and_act(_arr(150))  # < blip_ms
    await asyncio.sleep(0)
    ok &= _check("sub-blip noise -> NOT cancelled, NOT buffered",
                 not p._turn_cancelled and len(p._pending) == 0)
    p.turn_task.cancel()

    ok &= await _micro_ack_cases()

    print("\nALL PASS" if ok else "\nFAILURES")
    sys.exit(0 if ok else 1)


async def _micro_ack_cases() -> bool:
    """Latency-triggered micro-ack: fires only when the first sentence is slow."""
    ok = True
    runtime.engines.micro_acks = [("Okay.", b"a"), ("Sure.", b"b"), ("Right.", b"c")]

    def _pipe():
        p = _make_pipeline()
        p._ack_order = [0, 1, 2]
        p._ack_cursor = 0
        p._sent = []

        async def _fake_send(ulaw):
            p._sent.append(ulaw)
        p._send_paced = _fake_send
        return p

    # Fast turn: first sentence beats the threshold -> no ack.
    p = _pipe()
    fast = asyncio.create_task(_settle(0.0))
    ack = await p._maybe_micro_ack(fast, _now())
    await fast
    ok &= _check("fast turn -> no micro-ack", ack is None and len(p._sent) == 0)

    # Slow turn: first sentence misses the threshold (settings.ack_delay_ms) -> ack fires.
    p = _pipe()
    slow = asyncio.create_task(_settle((settings.ack_delay_ms + 800) / 1000))
    ack = await p._maybe_micro_ack(slow, _now())
    ok &= _check("slow turn -> micro-ack fires", ack in {"Okay.", "Sure.", "Right."}
                 and len(p._sent) == 1)
    slow.cancel()

    # Rotation: consecutive acks don't immediately repeat.
    p = _pipe()
    seq = [p._next_micro_ack() for _ in range(4)]
    ok &= _check("rotation cycles without immediate repeat",
                 len(set(seq[:3])) == 3 and seq[3] == seq[0])
    return ok


def _now() -> float:
    import time
    return time.perf_counter()


async def _settle(delay: float):
    await asyncio.sleep(delay)
    return ("first sentence.", _now())


if __name__ == "__main__":
    asyncio.run(main())
