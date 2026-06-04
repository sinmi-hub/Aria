"""Per-call orchestration: VAD -> STT -> Claude -> TTS, with barge-in.

One CallPipeline instance per active call. The media WebSocket feeds inbound
μ-law frames in; this class detects utterances, drives Claude, streams sentence
TTS back out, and interrupts itself when the caller talks over the agent.
"""
from __future__ import annotations

import asyncio
import random
import re
import time

import numpy as np

from config import (
    AGENT_GREETING,
    ERROR_FALLBACK,
    FILLER_REPROMPT,
    settings,
)

from app import trace
from app.agent import brain
from app.agent.sentence_chunker import sentences
from app.agent.tools import AriaTools
from app.audio import codec
from app.audio.streaming_stt import StreamingTranscriber
from app.audio.vad import UtteranceSegmenter
from app.log import debug, warn
from app.runtime import engines
from app.session import CallSession
from app.telephony import call_context

_FRAME_BYTES = int(settings.sample_rate_telnyx * settings.frame_ms / 1000)  # 160

_NORM_RE = re.compile(r"[^a-z0-9 ]")
_FILLERS = {"uh", "um", "er", "erm", "hmm", "mm", "ah"}


def _is_backchannel(text: str) -> bool:
    """True if an utterance is an acknowledgement (not a real interruption), per the
    config policy. Empty/garbled STT counts as backchannel — don't cut Aria off on it."""
    if not text:
        return True
    norm = _NORM_RE.sub("", text.lower()).strip()
    if not norm:
        return True
    if norm in settings.backchannel_phrases:
        return True
    words = [w for w in norm.split() if w not in _FILLERS]
    return bool(words) and all(w in settings.backchannel_words for w in words)


class CallPipeline:
    def __init__(self, session: CallSession, caller_phone: str | None = None,
                 outbound: bool = False) -> None:
        self.session = session
        self.caller_phone = caller_phone
        self.outbound = outbound
        self.segmenter = UtteranceSegmenter(engines.vad)
        # Phase 7 lever #1: overlap STT with speech so the transcript is ready at end-of-
        # speech. None when STT_STREAMING is off — the worker then transcribes as before.
        self.streaming_stt = StreamingTranscriber(engines.stt) if settings.stt_streaming else None
        self.utterances: asyncio.Queue = asyncio.Queue()
        self.turn_task: asyncio.Task | None = None
        self.speaking = False
        self._closed = False
        # Barge-in instrumentation (Pass 2 diagnostics — MEASURE, don't change behavior).
        self._audio_active = False       # True only while frames are actually being sent
        self._first_audio_sent = False   # per-turn: has any reply audio gone out yet?
        self._turn_started = 0.0         # perf_counter at the current turn's start
        self._barge_ins = 0
        self._silent_barge_ins = 0       # fired while NO audio was playing (the pathology)
        # Coalesce buffer: rapid caller utterances during an interruption are merged
        # into one turn once they settle (debounce), instead of restarting the turn
        # per utterance (the spiral).
        self._pending: list = []
        self._debounce_handle: asyncio.TimerHandle | None = None
        # Micro-ack rotation: a per-call shuffled order over the pre-rendered pool, so
        # consecutive slow turns don't repeat the same ack and calls don't all start the
        # same way. Cursor advances each time one fires.
        self._ack_order = random.sample(range(len(engines.micro_acks)), len(engines.micro_acks)) \
            if engines.micro_acks else []
        self._ack_cursor = 0
        self.tools = self._build_tools()
        self._worker = asyncio.create_task(self._turn_worker())

    def _build_tools(self) -> AriaTools | None:
        """Aria's CRM/Calendar tools. None (text-only) if Google isn't configured,
        so a misconfigured deploy still answers calls instead of failing."""
        has_creds = settings.google_sa_key_b64 or settings.google_sa_key_path
        if not (has_creds and settings.leads_sheet_id):
            return None
        try:
            return AriaTools(caller_phone=self.caller_phone)
        except Exception as exc:  # noqa: BLE001
            warn(f"[pipeline] tools unavailable, running text-only: {exc!r}")
            return None

    # --- inbound (called from the media WS recv loop) -------------------------
    def feed_inbound(self, ulaw_bytes: bytes) -> None:
        if self._closed:
            return
        audio = codec.ulaw8k_to_float16k(ulaw_bytes)
        for event in self.segmenter.push(audio):
            # We act on the COMPLETED utterance, not raw speech_start. speech_start
            # fires ~160ms in and (pre-Pass-2) cancelled turns during silent think-
            # time — the measured spiral. An utterance has a real duration we can
            # classify (backchannel vs interruption) and a transcript to act on.
            if event[0] == "speech_partial":
                if self.streaming_stt:
                    self.streaming_stt.feed(event[1])
            elif event[0] == "utterance":
                # Reuse the streamed transcript if it covered the utterance (≈0 STT at the
                # endpoint); else leave it to the worker. Read + reset synchronously so the
                # partial can't leak into the next utterance.
                pretranscript = None
                if self.streaming_stt:
                    pretranscript = self.streaming_stt.take_if_covers(event[1])
                    self.streaming_stt.reset()
                self._on_utterance(event[1], pretranscript)

    def _on_utterance(self, arr, pretranscript: str | None = None) -> None:
        """Route a completed caller utterance.

        Clean hand-off while Aria is idle -> queue immediately (worker transcribes;
        zero added latency on the good path). Otherwise (she holds the floor, or we're
        mid-coalesce) the decision is content-based, so defer to an async classifier.
        """
        if not (self.speaking or self._audio_active or self._pending):
            self.utterances.put_nowait((arr, pretranscript))
            return
        asyncio.create_task(self._guarded_classify(arr))

    async def _guarded_classify(self, arr) -> None:
        try:
            await self._classify_and_act(arr)
        except Exception as exc:  # noqa: BLE001 - a classifier failure must not crash the call
            trace.error("classify", exc)

    async def _classify_and_act(self, arr) -> None:
        """Content-aware turn arbitration while Aria holds the floor.

        Backchannels ('okay', 'yep', 'got it') must NOT stop her — duration alone
        misclassifies them (an 'okay' runs ~800ms, same as a real short interrupt),
        so we transcribe short/ambiguous utterances and match a policy word/phrase
        list. Long utterances are unambiguous real speech and supersede without waiting
        on STT. The transcript we compute here rides along so the worker never re-STTs.
        """
        dur_ms = 1000 * arr.size / settings.sample_rate_internal
        holding = self.speaking or self._audio_active
        transcript = None
        if holding:
            if dur_ms < settings.backchannel_blip_ms:
                debug(f"[turn] backchannel ignored (blip {dur_ms:.0f}ms)")
                return
            if dur_ms <= settings.hard_interrupt_ms:
                transcript = (await asyncio.to_thread(engines.stt.transcribe, arr)).strip()
                if _is_backchannel(transcript):
                    debug(f"[turn] backchannel ignored ({dur_ms:.0f}ms {transcript!r})")
                    return
            # CRITICAL: only cancel a turn that is actually SPEAKING (audio_active).
            # Cancelling during silent think-time starves the caller of audio — under
            # rapid talk the next turn is killed before it emits a frame, and they hear
            # nothing (the gpu-phase5e regression). During think we DON'T cancel: the
            # in-flight turn runs to first audio, and this utterance is buffered as a
            # follow-up turn. Guarantees first audio always escapes.
            if self._audio_active:
                self._supersede(dur_ms)
            else:
                debug(f"[turn] interruption during think — not cancelling, follow-up "
                      f"queued ({dur_ms:.0f}ms {transcript!r})")
        # Buffer + debounce: rapid correction utterances coalesce into one turn.
        self._pending.append((arr, transcript))
        self._arm_debounce()

    def _supersede(self, dur_ms: float) -> None:
        """Caller interrupted with real speech: stop audio and cancel the in-flight
        turn. Tool state (the looked-up lead) lives on AriaTools and survives, so the
        coalesced replacement turn doesn't redo work."""
        self._barge_ins += 1
        silent = not self._audio_active
        if silent:
            self._silent_barge_ins += 1
        debug(
            f"[supersede] {'during think' if silent else 'over audio'} "
            f"({dur_ms:.0f}ms utterance, first_audio_sent={self._first_audio_sent})"
        )
        self.speaking = False
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        asyncio.create_task(self.session.send_clear())

    def _arm_debounce(self) -> None:
        if self._debounce_handle:
            self._debounce_handle.cancel()
        loop = asyncio.get_event_loop()
        self._debounce_handle = loop.call_later(
            settings.coalesce_debounce_ms / 1000.0, self._commit_pending
        )

    def _commit_pending(self) -> None:
        if self._debounce_handle:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        if not self._pending:
            return
        n = len(self._pending)
        if n == 1:
            arr, transcript = self._pending[0]
        else:
            arr = np.concatenate([a for a, _ in self._pending])
            transcript = None  # re-transcribe the joined audio for accuracy
            debug(f"[turn] coalesced {n} utterances into one turn")
        self._pending = []
        self.utterances.put_nowait((arr, transcript))

    # --- greeting + turn loop -------------------------------------------------
    async def start_greeting(self) -> None:
        # Resolve outbound from the explicit flag or call_context (the webhook tags
        # the ccid when it places the call) so the opener works without media_ws changes.
        outbound = self.outbound or call_context.is_outbound(self.session.call_control_id)
        if self.tools:
            self.tools.outbound = outbound  # so call_context() pitches the right tone
        greeting = self._outbound_greeting() if outbound else AGENT_GREETING
        self.session.add_assistant(greeting)
        self.turn_task = asyncio.create_task(self._speak(greeting))

    def _outbound_greeting(self) -> str:
        """Context opening for an outbound call: identifies Aria + Settl and announces
        recording (TCPA). Personalize with the lead's first name when we have it."""
        name = ""
        if self.tools and self.caller_phone:
            try:
                lead = self.tools.store.get_lead_by_phone(self.caller_phone)
                if lead and lead.name:
                    name = lead.name.split()[0]
            except Exception as exc:  # noqa: BLE001 - lookup failure -> generic opener
                trace.error("outbound_lookup", exc)
        if name:
            return settings.outbound_greeting.format(name=name)
        return (
            "Hi, this is Aria calling on behalf of Settl, following up on your "
            "interest. Quick heads up, this call is recorded. Do you have a minute?"
        )

    async def _turn_worker(self) -> None:
        while not self._closed:
            item = await self.utterances.get()
            self.turn_task = asyncio.create_task(self._handle_turn(item))
            try:
                await self.turn_task
            except asyncio.CancelledError:
                pass

    async def _handle_turn(self, item) -> None:
        # item is (audio_array, transcript_or_None). The transcript is pre-computed
        # when classification already transcribed it, so we don't STT twice.
        arr, pretranscript = item
        # t0 = moment the utterance is dequeued, after the fixed VAD silence tail.
        t0 = time.perf_counter()
        self._turn_started = t0
        self._first_audio_sent = False
        # One fresh metrics dict per turn; every writer (brain TTFT, fast-ack,
        # first-answer, TTS) .update()s it so nobody clobbers another's keys.
        engines.last_metrics = {}
        trace.start_turn("outbound" if self.outbound else "inbound", self.caller_phone)
        dur_s = arr.size / settings.sample_rate_internal
        if pretranscript is None:
            transcript = (await asyncio.to_thread(engines.stt.transcribe, arr)).strip()
        else:
            transcript = pretranscript.strip()
        t_stt = time.perf_counter()
        trace.stage("stt", 1000 * (t_stt - t0))
        debug(f"[timing] stt={1000*(t_stt-t0):.0f}ms (utterance {dur_s:.1f}s)")
        debug(f"[pipeline] caller: {transcript!r}")
        if not transcript:
            await self._speak(FILLER_REPROMPT)
            return
        trace.set_caller(transcript)
        self.session.add_user(transcript)
        self.speaking = True
        spoken: list[str] = []
        first_sentence_task = None
        ack_sent = False
        ack_text = None
        first = True
        interrupted = False
        try:
            reply_sentences = sentences(brain.stream_reply(self.session.history, self.tools))
            first_sentence_task = asyncio.create_task(self._next_sentence(reply_sentences))
            # Latency-triggered micro-ack: only fires if the first sentence is slow to
            # arrive (a TTFT spike). Returns the ack text spoken, or None on a fast turn.
            ack_text = await self._maybe_micro_ack(first_sentence_task, t0)
            ack_sent = ack_text is not None

            first_item = await first_sentence_task
            if first_item is not None:
                sentence, t_sent = first_item
                await self._speak_reply_sentence(sentence, t0, t_stt, t_sent, first, ack_sent)
                spoken.append(sentence)
                first = False

            async for sentence in reply_sentences:
                t_sent = time.perf_counter()
                await self._speak_reply_sentence(sentence, t0, t_stt, t_sent, first, ack_sent)
                spoken.append(sentence)
                first = False
        except asyncio.CancelledError:
            interrupted = True
            if first_sentence_task and not first_sentence_task.done():
                first_sentence_task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - any LLM failure -> graceful fallback
            trace.error("llm", exc)
            await self._speak_sentence(ERROR_FALLBACK)
        finally:
            self.speaking = False
            # Record in `finally` so an interrupted turn still captures the partial
            # transcript AND writes what the caller heard to session history — else
            # Claude doesn't know it already said it and may repeat on the next turn.
            if spoken or ack_sent:
                heard_by_caller = ([ack_text] if ack_text else []) + spoken
                said = " ".join(heard_by_caller)
                self.session.add_assistant(said)
                trace.set_aria(said)
                if interrupted:
                    trace.mark_interrupted()
                debug(f"[pipeline] aria{' (interrupted)' if interrupted else ''}: {said!r}")

    async def _maybe_micro_ack(self, first_sentence_task: asyncio.Task, t0: float) -> str | None:
        """Cover a TTFT spike: if Claude's first sentence hasn't arrived within
        ack_delay_ms, play one short natural ack from the rotating pool so the caller
        isn't left in dead air, then the real answer streams in behind it. Returns the
        ack text if one fired, else None. Fast turns return None before the threshold,
        so normal turns never get an ack (that every-turn-ack is what felt robotic)."""
        if not self._ack_order:
            return None
        # Wait for the first sentence, but only up to the threshold. asyncio.wait does
        # NOT cancel the task on timeout, so it keeps streaming for the real reply.
        done, _ = await asyncio.wait({first_sentence_task},
                                     timeout=settings.ack_delay_ms / 1000)
        if first_sentence_task in done:
            return None  # arrived in time — no ack needed
        text, ulaw = engines.micro_acks[self._next_micro_ack()]
        caller_ms = 1000 * (time.perf_counter() - t0) + settings.vad_silence_threshold_ms
        engines.last_metrics.update({
            "ttfa_first_audio_ms": round(caller_ms),
            "ttfa_first_audio_source": "micro_ack",
        })
        trace.stage("ttfa", caller_ms)
        debug(f"[metric] ttfa_first_audio_ms={caller_ms:.0f} source=micro_ack text={text!r}")
        await self._send_paced(ulaw)
        return text

    def _next_micro_ack(self) -> int:
        idx = self._ack_order[self._ack_cursor % len(self._ack_order)]
        self._ack_cursor += 1
        return idx

    async def _next_sentence(self, stream) -> tuple[str, float] | None:
        try:
            sentence = await anext(stream)
        except StopAsyncIteration:
            return None
        return sentence, time.perf_counter()

    async def _speak_reply_sentence(
        self,
        sentence: str,
        t0: float,
        t_stt: float,
        t_sent: float,
        first: bool,
        first_audio_already_sent: bool,
    ) -> None:
        t_tts_start = time.perf_counter()
        audio, sr = await asyncio.to_thread(engines.tts.synthesize, sentence)
        if first:
            t_audio = time.perf_counter()
            answer_ms = 1000 * (t_audio - t0)
            caller_answer_ms = answer_ms + settings.vad_silence_threshold_ms
            llm_ms = 1000 * (t_sent - t_stt)
            tts_ms = 1000 * (t_audio - t_tts_start)
            if not first_audio_already_sent:
                engines.last_metrics.update({
                    "ttfa_first_audio_ms": round(caller_answer_ms),
                    "ttfa_first_audio_source": "answer",
                    "ttfa_first_audio_post_vad_ms": round(answer_ms),
                })
                trace.stage("ttfa", caller_answer_ms)
                debug(
                    f"[metric] ttfa_first_audio_ms={caller_answer_ms:.0f} "
                    f"post_vad_ms={answer_ms:.0f} source=answer"
                )
            engines.last_metrics.update({
                "ttfa_first_answer_ms": round(caller_answer_ms),
                "ttfa_first_answer_post_vad_ms": round(answer_ms),
                "llm_first_sentence_ms": round(llm_ms),
                "tts_first_answer_ms": round(tts_ms),
                "vad_silence_threshold_ms": settings.vad_silence_threshold_ms,
            })
            trace.stage("llm_first_sentence", llm_ms)
            trace.stage("tts_first", tts_ms)
            debug(
                f"[metric] ttfa_first_answer_ms={caller_answer_ms:.0f} "
                f"post_vad_ms={answer_ms:.0f} "
                f"llm_first_sentence_ms={llm_ms:.0f} "
                f"tts_first_answer_ms={tts_ms:.0f}"
            )
        if audio.size:
            await self._send_paced(codec.float_to_ulaw8k(audio, sr))

    # --- outbound audio -------------------------------------------------------
    async def _speak(self, text: str) -> None:
        self.speaking = True
        try:
            await self._speak_sentence(text)
        finally:
            self.speaking = False

    async def _speak_sentence(self, text: str) -> None:
        audio, sr = await asyncio.to_thread(engines.tts.synthesize, text)
        if audio.size == 0:
            return
        ulaw = codec.float_to_ulaw8k(audio, sr)
        await self._send_paced(ulaw)

    async def _send_paced(self, ulaw: bytes) -> None:
        """Send μ-law in 20ms frames at ~real time so barge-in stays responsive."""
        interval = settings.frame_ms / 1000.0
        self._audio_active = True
        self._first_audio_sent = True
        try:
            for frame in codec.chunk_ulaw(ulaw, _FRAME_BYTES):
                await self.session.send_media(codec.b64_encode(frame))
                await asyncio.sleep(interval * 0.92)
        finally:
            self._audio_active = False

    async def close(self) -> None:
        self._closed = True
        if self._debounce_handle:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        debug(
            f"[call-summary] supersedes={self._barge_ins} "
            f"during_think={self._silent_barge_ins} "
            f"over_audio={self._barge_ins - self._silent_barge_ins}"
        )
        for task in (self.turn_task, self._worker):
            if task and not task.done():
                task.cancel()
