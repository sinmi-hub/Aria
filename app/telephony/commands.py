"""Telnyx Call Control REST commands.

These drive the call lifecycle: answer the inbound call, then start bidirectional
media streaming to our WebSocket. Kept thin — one async function per action.
"""
from __future__ import annotations

import httpx

from app.log import debug, warn
from config import settings

_BASE = "https://api.telnyx.com/v2"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.telnyx_api_key}",
        "Content-Type": "application/json",
    }


async def _action(call_control_id: str, action: str, body: dict | None = None) -> None:
    url = f"{_BASE}/calls/{call_control_id}/actions/{action}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, headers=_headers(), json=body or {})
        if resp.status_code >= 300:
            warn(f"[telnyx] {action} -> {resp.status_code} {resp.text}")
        else:
            debug(f"[telnyx] {action} ok")


async def answer(call_control_id: str) -> None:
    await _action(call_control_id, "answer")


async def streaming_start(call_control_id: str) -> None:
    """Start bidirectional μ-law media streaming to our media WebSocket."""
    body = {
        "stream_url": settings.media_ws_url,
        "stream_track": "inbound_track",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "PCMU",
    }
    await _action(call_control_id, "streaming_start", body)


async def record_start(call_control_id: str) -> None:
    """Start Telnyx-hosted recording. Dual channel = caller + Aria on separate tracks."""
    await _action(call_control_id, "record_start", {"format": "mp3", "channels": "dual"})


async def hangup(call_control_id: str) -> None:
    await _action(call_control_id, "hangup")


async def speak(call_control_id: str, text: str) -> None:
    """Telnyx TTS — used to leave a scripted voicemail (no Kokoro needed here)."""
    body = {"payload": text, "voice": "female", "language": "en-US"}
    await _action(call_control_id, "speak", body)


# Cached Call Control application id (== the connection_id used to dial). Looked up
# once from Telnyx, then reused for the life of the process.
_connection_id: str | None = None


async def get_connection_id() -> str:
    """Find our Call Control application by name and return its id (connection_id)."""
    global _connection_id
    if _connection_id:
        return _connection_id
    url = f"{_BASE}/call_control_applications"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=_headers(), params={"page[size]": 100})
        resp.raise_for_status()
        for app in resp.json().get("data", []):
            if app.get("application_name") == settings.call_control_app_name:
                _connection_id = app["id"]
                debug(f"[telnyx] connection_id={_connection_id}")
                return _connection_id
    raise RuntimeError(
        f"no Call Control application named {settings.call_control_app_name!r}"
    )


async def dial(to: str, client_state_b64: str) -> str | None:
    """Place an outbound call. Returns the new call_control_id, or None on error.

    Uses the create-call endpoint (POST /v2/calls — NOT an /actions/ sub-path). No
    answering-machine detection: gating the greeting on AMD left human pickups in
    dead silence, so we greet immediately on answer (see webhook.py / TODO.md).
    """
    connection_id = await get_connection_id()
    body = {
        "connection_id": connection_id,
        "to": to,
        "from": settings.agent_number,
        "client_state": client_state_b64,
    }
    url = f"{_BASE}/calls"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, headers=_headers(), json=body)
        if resp.status_code >= 300:
            warn(f"[telnyx] dial -> {resp.status_code} {resp.text}")
            return None
        return resp.json().get("data", {}).get("call_control_id")
