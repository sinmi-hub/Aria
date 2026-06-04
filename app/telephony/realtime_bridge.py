"""Phase 6 PoC: bridge a Telnyx media call to the OpenAI Realtime API (speech-to-speech).

Drop-in for CallPipeline (same start_greeting / feed_inbound / close surface), selected by
REALTIME_BACKEND=openai in media_ws.py. Gate 0 confirmed Telnyx's g711 μ-law (audio/pcmu)
passes straight through, so this just shuttles base64 μ-law frames between two WebSockets:

    Telnyx media WS  ──feed_inbound(μ-law)──▶  _to_oa queue ──▶  input_audio_buffer.append
                     ◀──session.send_media────  output_audio.delta  ◀── OpenAI Realtime WS

PoC scope (PRD Phase 1): converse + barge-in + greeting + first-audio timing. NO tools yet
(Phase 2) — this exists to clear Gate 1 (live first_audio < 800ms from the pod). Pure I/O:
no torch/whisper/kokoro, so it's cheap to run.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

from app import trace
from app.agent.persona import system_prompt
from app.agent.tools import AriaTools
from app.log import debug, warn
from app.session import CallSession
from config import AGENT_GREETING, settings

_OA_URL = "wss://api.openai.com/v1/realtime?model={model}"


class RealtimeBridge:
    def __init__(self, session: CallSession, caller_phone: str | None = None,
                 outbound: bool = False) -> None:
        self.session = session
        self.caller_phone = caller_phone
        self.outbound = outbound
        self._oa = None                      # OpenAI realtime websocket
        self._to_oa: asyncio.Queue = asyncio.Queue()  # caller μ-law (b64) -> OpenAI
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._speech_stopped_at: float | None = None  # for first-audio timing
        self._awaiting_first_audio = False
        self._response_active = False  # only cancel a response that's actually running
        self.tools = self._build_tools()

    def _build_tools(self) -> AriaTools | None:
        """Same CRM/Calendar tools as the local pipeline; None (chat-only) if Google
        isn't configured, so a misconfigured deploy still answers."""
        has_creds = settings.google_sa_key_b64 or settings.google_sa_key_path
        if not (has_creds and settings.leads_sheet_id):
            return None
        try:
            t = AriaTools(caller_phone=self.caller_phone)
            t.outbound = self.outbound
            return t
        except Exception as exc:  # noqa: BLE001
            warn(f"[realtime] tools unavailable, chat-only: {exc!r}")
            return None

    def _tool_defs(self) -> list[dict]:
        """Anthropic schema {name,description,input_schema} -> realtime function defs."""
        return [{"type": "function", "name": s["name"],
                 "description": s.get("description", ""),
                 "parameters": s.get("input_schema", {})}
                for s in self.tools.schemas]

    # --- lifecycle (CallPipeline-compatible) ---------------------------------
    async def start_greeting(self) -> None:
        """Connect to OpenAI Realtime, configure the session (pcmu + server VAD + persona),
        start the audio pumps, and have Aria speak the greeting first (inbound call)."""
        import websockets

        if not settings.openai_api_key:
            warn("[realtime] OPENAI_API_KEY not set — cannot start realtime backend")
            return
        url = _OA_URL.format(model=settings.openai_realtime_model)
        self._oa = await websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            max_size=None)
        await self._oa.recv()  # session.created
        instructions = system_prompt() + (self.tools.call_context() if self.tools else "")
        session = {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": "whisper-1"},
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": settings.openai_voice},
            },
        }
        if self.tools:
            session["tools"] = self._tool_defs()
            session["tool_choice"] = "auto"
        await self._oa.send(json.dumps({"type": "session.update", "session": session}))
        self._tasks = [asyncio.create_task(self._pump_to_oa()),
                       asyncio.create_task(self._pump_from_oa())]
        # Greet first (inbound): ask for one response that speaks the greeting line.
        await self._oa.send(json.dumps({
            "type": "response.create",
            "response": {"instructions": f"Say exactly, warmly: {AGENT_GREETING}"},
        }))
        debug(f"[realtime] connected model={settings.openai_realtime_model} "
              f"voice={settings.openai_voice}")

    def feed_inbound(self, ulaw_bytes: bytes) -> None:
        """Called (sync) from the media-WS recv loop. Queue caller μ-law for the sender pump
        — keeps frame order and never blocks the recv loop on an await."""
        if self._closed or not ulaw_bytes:
            return
        self._to_oa.put_nowait(base64.b64encode(ulaw_bytes).decode())

    async def close(self) -> None:
        self._closed = True
        for t in self._tasks:
            t.cancel()
        if self._oa is not None:
            try:
                await self._oa.close()
            except Exception:  # noqa: BLE001
                pass
        debug("[realtime] closed")

    # --- pumps ----------------------------------------------------------------
    async def _pump_to_oa(self) -> None:
        """Drain queued caller audio to OpenAI in order."""
        try:
            while not self._closed:
                b64 = await self._to_oa.get()
                await self._oa.send(json.dumps(
                    {"type": "input_audio_buffer.append", "audio": b64}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            trace.error("realtime_send", exc)

    async def _pump_from_oa(self) -> None:
        """Read OpenAI events; forward output audio to Telnyx, handle barge-in + timing."""
        try:
            async for raw in self._oa:
                evt = json.loads(raw)
                et = evt.get("type", "")
                if et in ("response.output_audio.delta", "response.audio.delta"):
                    if self._awaiting_first_audio and self._speech_stopped_at is not None:
                        ms = 1000 * (time.perf_counter() - self._speech_stopped_at)
                        trace.stage("ttfa", ms)
                        debug(f"[metric] realtime first_audio_ms={ms:.0f} (ex-endpointing)")
                        self._awaiting_first_audio = False
                    delta = evt.get("delta")
                    if delta:
                        await self.session.send_media(delta)  # base64 μ-law straight through
                elif et == "response.created":
                    self._response_active = True
                elif et in ("response.done", "response.cancelled"):
                    self._response_active = False
                elif et == "input_audio_buffer.speech_started":
                    # Caller barged in — flush Telnyx playback and cancel the live response
                    # ONLY if one is running (else response.cancel errors not-active).
                    await self.session.send_clear()
                    if self._response_active:
                        await self._oa.send(json.dumps({"type": "response.cancel"}))
                        self._response_active = False
                elif et == "input_audio_buffer.speech_stopped":
                    self._speech_stopped_at = time.perf_counter()
                    self._awaiting_first_audio = True
                    trace.start_turn("outbound" if self.outbound else "inbound", self.caller_phone)
                elif et == "conversation.item.input_audio_transcription.completed":
                    trace.set_caller(evt.get("transcript", ""))
                elif et in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                    trace.set_aria(evt.get("transcript", ""))
                elif et == "response.function_call_arguments.done":
                    await self._run_tool(evt.get("name"), evt.get("call_id"),
                                         evt.get("arguments") or "{}")
                elif et == "error":
                    err = evt.get("error") or {}
                    # Benign: a barge-in fired response.cancel when nothing was playing.
                    if err.get("code") != "response_cancel_not_active":
                        trace.error("realtime", RuntimeError(str(err)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            trace.error("realtime_recv", exc)

    async def _run_tool(self, name: str | None, call_id: str | None, arguments: str) -> None:
        """Run a tool the model asked for, feed the result back, and let it continue
        speaking. Reuses AriaTools.dispatch (same logic as the local pipeline) — dispatch
        already records the call to the trace, so we don't trace.tool_call again here."""
        if not (self.tools and name and call_id):
            return
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            args = {}
        result = await self.tools.dispatch(name, args)
        await self._oa.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": result},
        }))
        await self._oa.send(json.dumps({"type": "response.create"}))
