"""Measurement-only probe for the OpenAI Realtime API — does NOT touch Aria's pipeline.

Question it answers: if we switched to speech-to-speech, what is the true time-to-first-
audio, and is it worth the rebuild (losing Kokoro voice + our tool orchestration + barge-in)?

It opens a raw Realtime WebSocket, sends one input (text by default, or an audio clip),
asks for a spoken response, and times:
  - first_text_ms   : response.create -> first transcript delta  (TTFT-equivalent)
  - first_audio_ms  : response.create -> first audio delta       (TTFA-equivalent, the number)
across N trials, reporting min / median / p95.

Compare against our current pipeline (Phase 5): perceived TTFA ~1.3s typical / ~1.05s floor,
of which the model portion (ex the 300ms VAD tail) is ~1.0s. With manual turn detection this
probe excludes endpointing on BOTH sides, so first_audio_ms is the fair model-vs-model number.

Setup:  export OPENAI_API_KEY=sk-...
Run:    .venv/bin/python -m scripts.realtime_probe              # text input, 5 trials
        .venv/bin/python -m scripts.realtime_probe --audio caller.wav --trials 8
        OPENAI_REALTIME_MODEL=gpt-4o-realtime-preview .venv/bin/python -m scripts.realtime_probe
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time

REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
INSTRUCTIONS = (
    "You are Aria, a warm, sharp phone sales agent for Settl (software for moving "
    "companies). Answer in one short spoken sentence. No markdown."
)
DEFAULT_TEXT = "How much does Settl cost?"


def _openai_key() -> str | None:
    """OPENAI_API_KEY from the process env, else from the gitignored deploy/.env
    (the project's secret store) so adding it there is enough to run this."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_file = os.path.join(os.path.dirname(__file__), "..", "deploy", ".env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


def _load_audio_b64(path: str) -> str:
    """Read a wav, resample to 24kHz mono pcm16 (Realtime's pcm16 format), base64 it."""
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 24000:
        n = int(round(len(audio) * 24000 / sr))
        audio = np.interp(np.linspace(0, len(audio), n, endpoint=False),
                          np.arange(len(audio)), audio).astype("float32")
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return base64.b64encode(pcm16).decode()


async def _trial(ws, text: str | None, audio_b64: str | None) -> dict:
    """Send one input, request a spoken response, time first text + first audio deltas."""
    if audio_b64 is not None:
        await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
    else:
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
        }))
    t0 = time.perf_counter()
    await ws.send(json.dumps({"type": "response.create"}))

    first_text_ms = first_audio_ms = None
    while first_audio_ms is None:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        evt = json.loads(raw)
        et = evt.get("type", "")
        if et in ("response.audio_transcript.delta", "response.output_audio_transcript.delta",
                  "response.text.delta") and first_text_ms is None:
            first_text_ms = 1000 * (time.perf_counter() - t0)
        elif et in ("response.audio.delta", "response.output_audio.delta"):
            first_audio_ms = 1000 * (time.perf_counter() - t0)
        elif et == "error":
            raise RuntimeError(f"realtime error: {evt.get('error')}")
        elif et == "response.done" and first_audio_ms is None:
            raise RuntimeError("response completed with no audio (check modalities/voice)")
    # Drain the rest of THIS response so the next trial doesn't read its trailing
    # audio deltas as a bogus ~0ms result.
    try:
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if evt.get("type") in ("response.done", "response.completed"):
                break
    except asyncio.TimeoutError:
        pass
    return {"first_text_ms": round(first_text_ms) if first_text_ms else None,
            "first_audio_ms": round(first_audio_ms)}


def _fmt(audio_format: str) -> dict:
    # GA realtime audio format objects. pcmu = g711 μ-law 8k (Telnyx-native -> Gate 0:
    # if this works, the bridge needs zero transcoding). pcm = 24k linear PCM.
    return {"type": "audio/pcmu"} if audio_format == "pcmu" else {"type": "audio/pcm", "rate": 24000}


async def run(model: str, text: str | None, audio_b64: str | None, trials: int,
              audio_format: str = "pcm") -> None:
    import websockets

    key = _openai_key()
    if not key:
        sys.exit("OPENAI_API_KEY not found (checked env + deploy/.env). "
                 "Add OPENAI_API_KEY=sk-... to deploy/.env or export it, then retry.")
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    # GA realtime: no OpenAI-Beta header (that triggers beta_api_shape_disabled).
    headers = {"Authorization": f"Bearer {key}"}
    mode = "audio" if audio_b64 else "text"
    print(f"[probe] model={model} input={mode} audio_format={audio_format} trials={trials}")

    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
        # Drain session.created, then configure GA-shape session: structured audio
        # object, output_modalities, manual turn detection (we control timing).
        await ws.recv()
        fmt = _fmt(audio_format)
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": INSTRUCTIONS,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {"format": fmt, "turn_detection": None},
                    "output": {"format": fmt, "voice": "alloy"},
                },
            },
        }))
        # Surface a session.update rejection (e.g. unsupported format) instead of hanging.
        try:
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if ack.get("type") == "error":
                raise RuntimeError(f"session.update rejected: {ack.get('error')}")
        except asyncio.TimeoutError:
            pass
        results = []
        for i in range(trials):
            try:
                r = await _trial(ws, text, audio_b64)
                results.append(r)
                print(f"  trial {i+1}: first_audio={r['first_audio_ms']}ms "
                      f"first_text={r['first_text_ms']}ms")
            except Exception as exc:  # noqa: BLE001
                print(f"  trial {i+1}: FAILED — {exc}")
            await asyncio.sleep(0.3)

    audio_ms = [r["first_audio_ms"] for r in results if r.get("first_audio_ms")]
    if not audio_ms:
        sys.exit("[probe] no successful trials.")
    audio_ms.sort()
    p95 = audio_ms[min(len(audio_ms) - 1, int(round(0.95 * (len(audio_ms) - 1))))]
    print("\n=== first_audio_ms (TTFA-equivalent, ex-endpointing) ===")
    print(f"  min={min(audio_ms)}  median={round(statistics.median(audio_ms))}  "
          f"p95={p95}  max={max(audio_ms)}  n={len(audio_ms)}")
    print("\n=== vs our pipeline ===")
    print("  ours: perceived TTFA ~1300ms typical (~1000ms ex-VAD-tail), ~1050ms floor.")
    med = round(statistics.median(audio_ms))
    delta = 1000 - med  # vs our ~1000ms model portion
    verdict = ("NEGLIGIBLE — not worth the rebuild" if delta < 200 else
               "MEANINGFUL — weigh against losing Kokoro voice + tool control")
    print(f"  realtime median {med}ms vs ~1000ms model-portion -> ~{delta}ms better. {verdict}")


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="realtime_probe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audio", help="wav clip to send as speech input (else text mode)")
    p.add_argument("--text", default=DEFAULT_TEXT, help="text input when no --audio")
    p.add_argument("--model", default=REALTIME_MODEL)
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--audio-format", choices=["pcm", "pcmu"], default="pcm",
                   help="pcmu = g711 μ-law 8k (Telnyx-native, Gate 0 check)")
    args = p.parse_args(argv)
    audio_b64 = _load_audio_b64(args.audio) if args.audio else None
    asyncio.run(run(args.model, None if audio_b64 else args.text, audio_b64, args.trials,
                    args.audio_format))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
