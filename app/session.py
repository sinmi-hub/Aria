"""Per-call state container: identifiers, conversation history, and the two
async sinks used to push audio back to Telnyx over the media WebSocket.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class CallSession:
    call_control_id: str
    stream_id: str
    send_media: Callable[[str], Awaitable[None]]  # arg: base64 μ-law frame
    send_clear: Callable[[], Awaitable[None]]      # flush Telnyx playback buffer
    history: list[dict] = field(default_factory=list)

    def add_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})
