"""Shared Google API plumbing: one service-account credential, lazily built
clients, and phone normalization used by both the Sheets and Calendar adapters.

The service account key is mounted (never baked into the image / committed) and
its path comes from config (``GOOGLE_SA_KEY_PATH``). Scopes cover both Sheets and
Calendar so a single credential drives both adapters.
"""
from __future__ import annotations

import re

from config import settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

_clients: dict[str, object] = {}


def _credentials():
    from google.oauth2 import service_account

    # Prefer the base64 env var (no-SSH hosts like the golden image); else a key file
    # (local dev / SSH-capable hosts). One of the two must be present.
    if settings.google_sa_key_b64:
        import base64
        import json

        info = json.loads(base64.b64decode(settings.google_sa_key_b64))
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if settings.google_sa_key_path:
        return service_account.Credentials.from_service_account_file(
            settings.google_sa_key_path, scopes=SCOPES
        )
    raise RuntimeError(
        "No Google credentials: set GOOGLE_SA_KEY_B64 or GOOGLE_SA_KEY_PATH."
    )


def service(api: str, version: str):
    """Return a cached googleapiclient service (e.g. service('sheets', 'v4'))."""
    cache_key = f"{api}:{version}"
    if cache_key not in _clients:
        from googleapiclient.discovery import build

        _clients[cache_key] = build(
            api, version, credentials=_credentials(), cache_discovery=False
        )
    return _clients[cache_key]


def normalize_phone(raw: str) -> str:
    """Best-effort E.164 for US numbers. '15557654321' / '(443) 929-2703' -> '+15557654321'.

    Leaves already-+-prefixed numbers alone (minus stray formatting). Aria's leads
    are US moving companies, so a bare 10-digit number gets a +1.
    """
    if not raw:
        return ""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else ""
