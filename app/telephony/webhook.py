"""Telnyx Call Control HTTP webhook.

Telnyx POSTs call lifecycle events here. We answer inbound calls, then kick off
bidirectional media streaming once the call is answered. Everything after that
happens over the media WebSocket (see media_ws.py).

When TELNYX_PUBLIC_KEY is configured, every request is Ed25519 signature-verified
(see signature.py) and unsigned/forged requests are rejected with 401.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from app.integrations.crm import SheetsLeadStore
from app.log import debug, warn
from app.telephony import call_context, commands, signature

router = APIRouter()


@router.post("/telnyx/webhook")
async def telnyx_webhook(request: Request) -> dict:
    raw = await request.body()
    if not signature.verify(
        raw,
        request.headers.get("telnyx-signature-ed25519", ""),
        request.headers.get("telnyx-timestamp", ""),
    ):
        raise HTTPException(status_code=401, detail="invalid Telnyx signature")

    body = json.loads(raw or b"{}")
    data = body.get("data", {})
    event = data.get("event_type", "")
    payload = data.get("payload", {})
    ccid = payload.get("call_control_id", "")
    debug(f"[webhook] {event} ccid={ccid[:12]}... dir={payload.get('direction')}")

    if event == "call.initiated":
        direction = payload.get("direction")
        if direction == "incoming":
            call_context.remember(ccid, payload.get("from", ""))  # for lead lookup
            await commands.answer(ccid)
        elif direction == "outgoing":
            # We placed this call; don't answer (Telnyx auto-progresses). Just tag
            # it so call.answered/AMD branch outbound and the pipeline greets in context.
            state = call_context.decode_state(payload.get("client_state", ""))
            if state.get("outbound"):
                call_context.remember(ccid, state.get("phone", ""))
                call_context.mark_outbound(ccid)
    elif event == "call.answered":
        # Greet immediately on answer for BOTH directions. (We dropped AMD-gated
        # outbound: gating on answering-machine detection left a human picker-upper
        # in dead silence for several seconds, so they hung up before Aria spoke.
        # The pipeline still opens with outbound context via call_context.is_outbound;
        # if a machine answers, Aria's spoken greeting serves as the message.
        # Smart machine-detection voicemail is a parked refinement — see TODO.md.)
        await commands.streaming_start(ccid)
        await commands.record_start(ccid)
    elif event == "call.recording.saved":
        _save_recording(ccid, payload)
        call_context.forget(ccid)  # recording.saved arrives after hangup — forget here

    return {}


def _save_recording(ccid: str, payload: dict) -> None:
    """Write the Telnyx-hosted mp3 URL to the calling lead's recording_url cell."""
    urls = payload.get("recording_urls") or {}
    public = payload.get("public_recording_urls") or {}
    url = urls.get("mp3") or public.get("mp3")
    if not url:
        warn("[webhook] recording.saved with no mp3 url")
        return
    phone = call_context.caller_phone(ccid)
    if not phone:
        warn(f"[webhook] recording.saved but no caller phone for ccid={ccid[:12]}...")
        return
    store = SheetsLeadStore()
    lead = store.get_lead_by_phone(phone)
    if not lead:
        warn(f"[webhook] recording.saved but no lead for phone {phone}")
        return
    store.set_recording_url(lead, url)
    debug(f"[webhook] recording url written to row {lead.row}")
