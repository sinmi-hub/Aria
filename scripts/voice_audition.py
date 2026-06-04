"""Render a fixed Aria line in each candidate OpenAI Realtime voice -> wav files.

Auditioning by redeploying the pod once per voice is slow; this renders samples offline
so you just listen and pick. Writes audition_<voice>.wav (24k pcm16) for each voice, then
set OPENAI_VOICE=<pick> in deploy/.env and redeploy once.

Setup:  OPENAI_API_KEY in env or deploy/.env (the probe's loader is reused).
Run:    .venv/bin/python -m scripts.voice_audition
        .venv/bin/python -m scripts.voice_audition --voices marin,cedar,coral --out /tmp/aud
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys

from scripts.realtime_probe import _openai_key

# gpt-realtime GA voices; marin + cedar are the newest/most natural. Warm female-leaning
# ones (closest to Kokoro af_heart): marin, coral, shimmer, sage.
DEFAULT_VOICES = ["marin", "cedar", "coral", "sage", "shimmer", "alloy"]
SAMPLE = ("Hi, this is Aria with Settl! Good to hear from you — your demo's all set for "
          "Monday at noon. Was there anything else I can help you with today?")
MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")


async def _render(voice: str, out_dir: str) -> str | None:
    import numpy as np
    import soundfile as sf
    import websockets

    key = _openai_key()
    url = f"wss://api.openai.com/v1/realtime?model={MODEL}"
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {key}"}, max_size=None) as ws:
        await ws.recv()  # session.created
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                # pcm 24k for a clean wav to judge the voice (not telephony μ-law).
                "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000},
                                     "voice": voice}},
            },
        }))
        await ws.send(json.dumps({
            "type": "response.create",
            "response": {"instructions": f"Say exactly, warmly: {SAMPLE}"},
        }))
        pcm = bytearray()
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            et = evt.get("type", "")
            if et in ("response.output_audio.delta", "response.audio.delta"):
                pcm += base64.b64decode(evt["delta"])
            elif et == "error":
                print(f"  {voice}: ERROR {evt.get('error')}")
                return None
            elif et in ("response.done", "response.completed"):
                break
    if not pcm:
        print(f"  {voice}: no audio")
        return None
    samples = np.frombuffer(bytes(pcm), dtype="<i2")
    path = os.path.join(out_dir, f"audition_{voice}.wav")
    sf.write(path, samples, 24000, subtype="PCM_16")
    print(f"  {voice}: {path}  ({len(samples)/24000:.1f}s)")
    return path


async def run(voices: list[str], out_dir: str) -> None:
    if not _openai_key():
        sys.exit("OPENAI_API_KEY not found (env or deploy/.env).")
    os.makedirs(out_dir, exist_ok=True)
    print(f"[audition] model={MODEL} voices={voices}\n  line: {SAMPLE!r}")
    for v in voices:
        try:
            await _render(v, out_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"  {v}: FAILED — {exc}")
    print(f"\nDone. Listen to {out_dir}/audition_*.wav, then set OPENAI_VOICE=<pick> "
          f"in deploy/.env and redeploy.")


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="voice_audition", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--voices", default=",".join(DEFAULT_VOICES))
    p.add_argument("--out", default="/tmp/voice-audition")
    args = p.parse_args(argv)
    asyncio.run(run([v.strip() for v in args.voices.split(",") if v.strip()], args.out))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
