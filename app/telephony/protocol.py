"""Telnyx media-streaming WebSocket wire protocol.

Telnyx sends/receives JSON text frames over the media socket. Inbound: ``start``,
``media`` (base64 μ-law), ``stop``. Outbound (bidirectional): ``media`` to play
audio to the caller, ``clear`` to flush the playback buffer (barge-in).
Docs: https://developers.telnyx.com/docs/voice/programmable-voice/media-streaming
"""
from __future__ import annotations

import json
from typing import Any


def parse(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def event_type(msg: dict) -> str:
    return msg.get("event", "")


def stream_id(msg: dict) -> str:
    return msg.get("stream_id", "")


def call_control_id(start_msg: dict) -> str:
    return start_msg.get("start", {}).get("call_control_id", "")


def inbound_payload(media_msg: dict) -> str | None:
    """Base64 μ-law payload. We stream only the inbound track (configured in
    streaming_start), so accept any track label rather than guess Telnyx's name."""
    return media_msg.get("media", {}).get("payload")


def media_frame(payload_b64: str, sid: str) -> str:
    msg = {"event": "media", "media": {"payload": payload_b64}}
    if sid:
        msg["stream_id"] = sid
    return json.dumps(msg)


def clear_frame(sid: str) -> str:
    msg = {"event": "clear"}
    if sid:
        msg["stream_id"] = sid
    return json.dumps(msg)
