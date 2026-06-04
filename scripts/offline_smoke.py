"""Offline pipeline check — no phone, no Telnyx, no tunnel.

Exercises every local stage so we know the audio path works before a live call:
  TTS -> μ-law 8k -> back to 16k float -> Whisper STT  (codec + STT + TTS round-trip)
  VAD over that audio                                   (utterance detection)
  Claude streaming -> sentence chunker                  (LLM path)

Run with: make smoke
"""
from __future__ import annotations

import asyncio

import numpy as np

from app.agent import brain
from app.agent.sentence_chunker import sentences
from app.audio import codec
from app.audio.vad import UtteranceSegmenter
from app.runtime import engines


def check_audio_roundtrip() -> None:
    print("\n[1/3] TTS -> codec -> STT round-trip")
    phrase = "Hello, can you hear me clearly on this phone call?"
    audio, sr = engines.tts.synthesize(phrase)
    print(f"   TTS produced {audio.size} samples @ {sr}Hz")
    assert audio.size > 0, "TTS produced no audio"

    ulaw = codec.float_to_ulaw8k(audio, sr)
    back = codec.ulaw8k_to_float16k(ulaw)
    print(f"   μ-law bytes: {len(ulaw)}; decoded 16k samples: {back.size}")

    transcript = engines.stt.transcribe(back)
    print(f"   said:  {phrase!r}")
    print(f"   heard: {transcript!r}")
    assert transcript, "STT returned empty string"


def check_vad() -> None:
    print("\n[2/3] VAD utterance segmentation")
    audio, sr = engines.tts.synthesize("This is a short test sentence.")
    speech16k = codec.ulaw8k_to_float16k(codec.float_to_ulaw8k(audio, sr))
    tail = np.zeros(16000, dtype=np.float32)  # 1s trailing silence -> utterance end
    seg = UtteranceSegmenter(engines.vad)
    events = seg.push(speech16k) + seg.push(tail)
    kinds = [e[0] for e in events]
    print(f"   events: {kinds}")
    assert "utterance" in kinds, "VAD never detected an utterance end"


async def check_claude() -> None:
    print("\n[3/3] Claude streaming -> sentence chunker")
    history = [{"role": "user", "content": "Say a friendly one-sentence hello."}]
    out = [s async for s in sentences(brain.stream_reply(history))]
    print(f"   sentences: {out}")
    assert out, "Claude produced no output"


def main() -> None:
    engines.load()
    check_audio_roundtrip()
    check_vad()
    asyncio.run(check_claude())
    print("\nAll offline checks passed.\n")


if __name__ == "__main__":
    main()
