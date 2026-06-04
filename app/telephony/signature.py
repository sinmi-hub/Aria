"""Telnyx webhook signature verification (Ed25519).

Telnyx signs each webhook with an Ed25519 signature over ``f"{timestamp}|{raw_body}"``
and sends two headers: ``telnyx-signature-ed25519`` (base64 signature) and
``telnyx-timestamp`` (unix seconds). We verify with the account's base64 public key
(Telnyx portal -> Account -> Keys & Credentials -> Public Key), and reject stale
timestamps to block replay.

Verification is only enforced when ``settings.telnyx_public_key`` is set; otherwise
``verify`` returns True so dev / un-keyed setups keep working.
"""
from __future__ import annotations

import base64
import time

from config import settings

# 5 minutes; generous enough for clock skew, tight enough to stop replays.
_TOLERANCE_S = 300


def verify(raw_body: bytes, signature_b64: str, timestamp: str) -> bool:
    """Return True iff the request is authentic (or verification is disabled)."""
    if not settings.telnyx_public_key:
        return True  # not configured -> skip (back-compat)
    if not signature_b64 or not timestamp:
        return False
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:  # pragma: no cover - dependency missing
        # Fail closed: a key was configured but we can't verify -> reject.
        print("[signature] PyNaCl not installed but TELNYX_PUBLIC_KEY set; rejecting")
        return False

    try:
        if abs(time.time() - int(timestamp)) > _TOLERANCE_S:
            return False  # stale -> likely replay
    except (TypeError, ValueError):
        return False

    signed = f"{timestamp}|".encode() + raw_body
    try:
        key = VerifyKey(base64.b64decode(settings.telnyx_public_key))
        key.verify(signed, base64.b64decode(signature_b64))
        return True
    except (BadSignatureError, ValueError):
        return False
