"""Telnyx media-streaming WebSocket endpoint (/ws/media).

Accepts the bidirectional audio socket Telnyx opens after ``streaming_start``.
Wires each call to a CallPipeline and shuttles audio both ways.

When MEDIA_WS_TOKEN is configured, the socket requires a matching ``?token=``
query param (setup_telnyx bakes it into the stream_url Telnyx dials); unauthorized
connections are closed before any audio flows.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.pipeline import CallPipeline
from app.audio import codec
from app.log import debug
from app.session import CallSession
from app.telephony import call_context, protocol
from app.telephony.realtime_bridge import RealtimeBridge
from config import settings

router = APIRouter()


@router.websocket("/ws/media")
async def media_ws(ws: WebSocket) -> None:
    if settings.media_ws_token and ws.query_params.get("token") != settings.media_ws_token:
        debug("[media_ws] rejected: bad/missing token")
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    pipeline: CallPipeline | RealtimeBridge | None = None
    debug("[media_ws] connected")
    try:
        while True:
            msg = protocol.parse(await ws.receive_text())
            event = protocol.event_type(msg)

            if event == "start":
                pipeline = _begin_call(ws, msg)
                await pipeline.start_greeting()
            elif event == "media" and pipeline is not None:
                payload = protocol.inbound_payload(msg)
                if payload:
                    pipeline.feed_inbound(codec.b64_decode(payload))
            elif event == "stop":
                break
    except WebSocketDisconnect:
        debug("[media_ws] disconnected")
    finally:
        if pipeline is not None:
            await pipeline.close()
        debug("[media_ws] closed")


def _begin_call(ws: WebSocket, start_msg: dict) -> CallPipeline | RealtimeBridge:
    sid = protocol.stream_id(start_msg)
    ccid = protocol.call_control_id(start_msg)
    debug(f"[media_ws] stream start sid={sid[:12]}...")

    async def send_media(payload_b64: str) -> None:
        try:
            await ws.send_text(protocol.media_frame(payload_b64, sid))
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def send_clear() -> None:
        try:
            await ws.send_text(protocol.clear_frame(sid))
        except (WebSocketDisconnect, RuntimeError):
            pass

    session = CallSession(ccid, sid, send_media, send_clear)
    phone = call_context.caller_phone(ccid)  # captured by the webhook for lead lookup
    debug(f"[media_ws] caller_phone={phone or '<unknown>'} backend={settings.realtime_backend}")
    if settings.realtime_backend == "openai":
        return RealtimeBridge(session, caller_phone=phone)
    return CallPipeline(session, caller_phone=phone)
