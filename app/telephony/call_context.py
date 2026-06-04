"""Bridge the caller's phone number from the HTTP webhook to the media WebSocket.

Telnyx delivers the caller's ``from`` number in the call.initiated webhook, but the
media socket only gets a ``call_control_id``. We stash {ccid: from_number} here when
the webhook fires so media_ws can hand the number to AriaTools for lead lookup.

In-memory and best-effort: one Lambda/process handles a call end to end. Entries are
dropped on hangup (and the dict is tiny), so no real growth concern for the pilot.
"""
from __future__ import annotations

import base64
import json

_caller_numbers: dict[str, str] = {}
# ccids we placed ourselves (outbound). Drives the AMD/voicemail branch in the
# webhook and Aria's context opening in the pipeline.
_outbound: dict[str, bool] = {}


def remember(ccid: str, from_number: str) -> None:
    if ccid and from_number:
        _caller_numbers[ccid] = from_number


def caller_phone(ccid: str) -> str | None:
    return _caller_numbers.get(ccid)


def mark_outbound(ccid: str) -> None:
    if ccid:
        _outbound[ccid] = True


def is_outbound(ccid: str) -> bool:
    return _outbound.get(ccid, False)


def forget(ccid: str) -> None:
    _caller_numbers.pop(ccid, None)
    _outbound.pop(ccid, None)


# --- client_state: carried on the dialed call and echoed back on every event ----
def encode_state(phone: str) -> str:
    """base64(JSON) marking an outbound call and carrying the lead's phone."""
    raw = json.dumps({"outbound": True, "phone": phone}).encode()
    return base64.b64encode(raw).decode()


def decode_state(b64: str) -> dict:
    """Reverse encode_state; {} on any error (missing/garbled client_state)."""
    if not b64:
        return {}
    try:
        return json.loads(base64.b64decode(b64).decode())
    except Exception:  # noqa: BLE001 - any decode failure -> treat as no state
        return {}
