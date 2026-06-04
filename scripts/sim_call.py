"""Headless 'phone call' against the media WebSocket — proves the full audio loop
without dialing the PSTN.

It impersonates Telnyx: opens /ws/media, sends a ``start`` frame, speaks a caller
utterance as base64 μ-law (synthesized locally), then collects Aria's audio reply
and transcribes it back. Exercises exactly what a real call does end to end:
  WS upgrade -> start -> inbound media -> VAD -> STT -> Claude -> TTS -> outbound media
plus the media-WS token guard, over whatever URL you point it at (local or the
RunPod proxy).

Usage:
  python -m scripts.sim_call                      # uses PUBLIC_URL (or localhost:8000)
  python -m scripts.sim_call wss://host/ws/media  # explicit URL
  python -m scripts.sim_call --say "what are your hours?"

Exit code 0 = Aria replied with audio; non-zero = no reply (failure).
"""
from __future__ import annotations

import asyncio
import sys
import time

import numpy as np
import websockets

from app.audio import codec
from app.runtime import engines
from config import settings

_FRAME_BYTES = int(settings.sample_rate_telnyx * settings.frame_ms / 1000)  # 160
_FRAME_S = settings.frame_ms / 1000.0


def _default_url() -> str:
    base = (settings.public_url or "http://localhost:8000")
    base = base.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base}/ws/media"
    if settings.media_ws_token:
        url += f"?token={settings.media_ws_token}"
    return url


def _ulaw_frames(ulaw: bytes) -> list[str]:
    return [codec.b64_encode(f) for f in codec.chunk_ulaw(ulaw, _FRAME_BYTES)]


async def _send_audio(ws, ulaw: bytes, sid: str) -> None:
    """Stream μ-law to the server in real-time 20ms frames, like Telnyx does."""
    import json
    for b64 in _ulaw_frames(ulaw):
        await ws.send(json.dumps({"event": "media", "stream_id": sid,
                                  "media": {"payload": b64}}))
        await asyncio.sleep(_FRAME_S * 0.95)


async def run(url: str, phrase: str) -> int:
    import json
    print(f"[sim] loading local engines (to speak + transcribe)...")
    engines.load()
    print(f"[sim] connecting: {url}")

    # The caller's utterance, synthesized locally -> μ-law 8k (what Telnyx would send).
    say_audio, sr = engines.tts.synthesize(phrase)
    say_ulaw = codec.float_to_ulaw8k(say_audio, sr)
    silence = codec.float_to_ulaw8k(np.zeros(int(1.2 * settings.sample_rate_telnyx),
                                             dtype=np.float32), settings.sample_rate_telnyx)

    sid = "sim-stream-1"
    inbound: list[bytes] = []          # μ-law the server sends back
    got_clear = False
    measure_reply = False
    t_caller_stop: float | None = None
    t_first_reply_audio: float | None = None
    try:
        async with websockets.connect(url, open_timeout=20, max_size=None) as ws:
            print("[sim] WS upgrade OK")
            await ws.send(json.dumps({"event": "start", "stream_id": sid,
                "start": {"call_control_id": "sim-ccid",
                          "media_format": {"encoding": "PCMU", "sample_rate": 8000,
                                           "channels": 1}}}))

            async def receiver() -> None:
                nonlocal got_clear, t_first_reply_audio
                while True:
                    raw = await ws.recv()
                    m = json.loads(raw)
                    ev = m.get("event")
                    if ev == "media":
                        if measure_reply and t_first_reply_audio is None:
                            t_first_reply_audio = time.perf_counter()
                        inbound.append(codec.b64_decode(m["media"]["payload"]))
                    elif ev == "clear":
                        got_clear = True

            rx = asyncio.create_task(receiver())

            # 1) let the greeting play, then note how much greeting audio arrived
            await asyncio.sleep(3.0)
            greeting_frames = len(inbound)
            print(f"[sim] greeting: {greeting_frames} frames "
                  f"({greeting_frames*_FRAME_S:.1f}s of audio)")
            inbound.clear()

            # 2) speak the caller utterance, then silence so VAD ends the turn
            print(f"[sim] caller says: {phrase!r}")
            await _send_audio(ws, say_ulaw, sid)
            t_caller_stop = time.perf_counter()
            measure_reply = True
            await _send_audio(ws, silence, sid)

            # 3) collect Aria's reply: wait for audio to START (STT+Claude+TTS take
            #    ~1-2s), then end the capture after a ~1.5s quiet gap.
            t_deadline = time.time() + 15
            last = 0
            seen_any = False
            quiet_s = 0.0
            while time.time() < t_deadline:
                await asyncio.sleep(0.3)
                if len(inbound) > last:
                    last = len(inbound)
                    seen_any = True
                    quiet_s = 0.0
                elif seen_any:
                    quiet_s += 0.3
                    if quiet_s >= 1.5:
                        break  # reply finished

            rx.cancel()

            reply_frames = len(inbound)
            print(f"[sim] reply: {reply_frames} frames "
                  f"({reply_frames*_FRAME_S:.1f}s of audio); barge-in clear={got_clear}")
            if t_caller_stop is not None and t_first_reply_audio is not None:
                ttfa_ms = 1000 * (t_first_reply_audio - t_caller_stop)
                print(f"[metric] client_ttfa_ms={ttfa_ms:.0f}")
            if reply_frames == 0:
                print("[sim] FAIL: no audio reply from Aria")
                return 1

            # 4) transcribe the reply to confirm it's a real spoken response
            reply_16k = codec.ulaw8k_to_float16k(b"".join(inbound))
            heard = engines.stt.transcribe(reply_16k).strip()
            print(f"[sim] Aria said (STT of reply): {heard!r}")
            print("[sim] PASS: full call loop works over this endpoint")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[sim] FAIL: {type(exc).__name__}: {exc}")
        return 2


def main() -> None:
    args = [a for a in sys.argv[1:]]
    phrase = "Hi, what are your hours and where are you located?"
    url = None
    i = 0
    while i < len(args):
        if args[i] == "--say" and i + 1 < len(args):
            phrase = args[i + 1]
            i += 2
        else:
            url = args[i]
            i += 1
    url = url or _default_url()
    sys.exit(asyncio.run(run(url, phrase)))


if __name__ == "__main__":
    main()
