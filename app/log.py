"""Tiny logging helpers + an in-memory ring of recent lines.

The ring backs /debug/logs so we can read a call's logs over the proxy without SSH
(the golden image has no sshd). Every debug/warn line is captured in the ring even
when DEBUG is off, so /debug/logs always reflects the latest call.
"""
from __future__ import annotations

import sys
from collections import deque
from datetime import datetime, timezone

_RING: deque[str] = deque(maxlen=600)


def _stamp(msg: str) -> str:
    # Wall-clock prefix so /debug/logs lines link to the recording + turn timestamps.
    return f"{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {msg}"


def debug(msg: str) -> None:
    _RING.append(_stamp(msg))
    if _enabled():
        print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    _RING.append(_stamp(msg))
    print(msg, file=sys.stderr, flush=True)


def recent(n: int = 200) -> list[str]:
    return list(_RING)[-n:]


def _enabled() -> bool:
    from config import settings
    return settings.debug
