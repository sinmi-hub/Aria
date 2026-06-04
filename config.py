"""Central configuration. All tunable knobs live here (per the brief).

Values come from environment variables (loaded from .env) with sane defaults so
the system runs out of the box. Nothing secret is hard-coded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # reads ./.env


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_bool(name: str, default: bool = False) -> bool:
    return _get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _get_tuple(name: str, default: tuple) -> tuple:
    """Comma-separated env override -> lowercased tuple; else the default policy."""
    raw = _get(name, "").strip()
    if not raw:
        return default
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _public_url() -> str:
    """Explicit PUBLIC_URL wins; otherwise, when running as a RunPod pod (the golden
    image), derive the stable proxy URL from the injected RUNPOD_POD_ID so the
    container is self-configuring — no need to know the pod id before creating it."""
    explicit = _get("PUBLIC_URL").rstrip("/")
    if explicit:
        return explicit
    pod_id = _get("RUNPOD_POD_ID")
    port = _get("PORT", "8000")
    if pod_id:
        return f"https://{pod_id}-{port}.proxy.runpod.net"
    return ""


# --- Agent persona (edit freely) -------------------------------------------
AGENT_NAME = "Aria"
AGENT_GREETING = (
    "Hi, this is Aria with Settl. Just so you know, this call is recorded. "
    "What can I help you with?"
)
AGENT_SYSTEM_PROMPT = (
    f"You are {AGENT_NAME}, a warm, helpful voice assistant on a phone call. "
    "You are speaking out loud, so keep replies short and conversational, usually "
    "one or two sentences. Never use markdown, bullet points, emoji, or special "
    "characters; write plain spoken words only. A brief acknowledgement may already "
    "be played after the caller stops, so do not repeat filler like got it at the "
    "start. Make your first sentence short and useful, with an early sentence "
    "boundary. If you do not understand, ask the caller to repeat. Be natural and "
    "friendly."
)
# Said when STT returns nothing intelligible.
FILLER_REPROMPT = "Sorry, I didn't catch that. Could you say it again?"
# Said when Claude errors.
ERROR_FALLBACK = "Sorry, I'm having a little trouble right now. Could you repeat that?"
# Optional filler played immediately after STT while Claude streams. Default OFF —
# repeated "Got it." every turn sounds robotic. Set FAST_ACKNOWLEDGEMENT to re-enable.
FAST_ACKNOWLEDGEMENT = _get("FAST_ACKNOWLEDGEMENT", "").strip()


@dataclass(frozen=True)
class Settings:
    # --- Secrets (from env) ---
    anthropic_api_key: str = field(default_factory=lambda: _get("ANTHROPIC_API_KEY"))
    telnyx_api_key: str = field(default_factory=lambda: _get("TELNYX_API"))

    # Telnyx webhook signing public key (base64 Ed25519), from the Telnyx portal /
    # Mission Control. When set, inbound webhooks are signature-verified; when empty,
    # verification is skipped (dev/back-compat). See app/telephony/webhook.py.
    telnyx_public_key: str = field(default_factory=lambda: _get("TELNYX_PUBLIC_KEY"))

    # Shared secret guarding the media WebSocket. When set, /ws/media requires
    # ?token=<this>; setup_telnyx bakes it into the stream_url Telnyx dials. Empty
    # = open socket (dev/back-compat).
    media_ws_token: str = field(default_factory=lambda: _get("MEDIA_WS_TOKEN"))

    # Verbose per-turn [timing]/[pipeline] logs. Off by default for prod; flip with DEBUG=1.
    debug: bool = field(default_factory=lambda: _get_bool("DEBUG", False))

    # Public base URL Telnyx streams to. Explicit PUBLIC_URL (cloudflared tunnel or
    # RunPod proxy); else auto-derived from RUNPOD_POD_ID inside a RunPod pod.
    public_url: str = field(default_factory=_public_url)

    # --- LLM ---
    model: str = field(default_factory=lambda: _get("MODEL", "claude-haiku-4-5"))
    max_tokens: int = 160  # backstop only — brevity comes from the persona, not this cap
                            # (120 guillotined real replies mid-sentence in testing)
    temperature: float = 0.6

    # --- Backend select (Phase 6) ---
    # "local" = the Phase 5 VAD->STT->Claude->TTS pipeline (default, untouched).
    # "openai" = the OpenAI Realtime speech-to-speech bridge (app/telephony/realtime_bridge.py).
    realtime_backend: str = field(default_factory=lambda: _get("REALTIME_BACKEND", "local"))
    openai_api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    openai_realtime_model: str = field(
        default_factory=lambda: _get("OPENAI_REALTIME_MODEL", "gpt-realtime"))
    openai_voice: str = field(default_factory=lambda: _get("OPENAI_VOICE", "alloy"))

    # --- Cascade SLM brain (Phase 7) ---
    # "claude" = the network Claude brain (default, untouched). "slm" = the local Qwen brain
    # (app/agent/slm_brain.py) selected for the LOCAL pipeline only — needs the GPU pod
    # (transformers + CUDA). SLM-only for now: NO Claude escalation, so we measure the pure
    # SLM latency floor first ("how far can we go"). bf16 beat 4-bit on the A4500 (probe).
    brain_backend: str = field(default_factory=lambda: _get("BRAIN_BACKEND", "claude"))
    slm_model: str = field(
        default_factory=lambda: _get("SLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507"))
    slm_dtype: str = field(default_factory=lambda: _get("SLM_DTYPE", "bf16"))  # bf16|4bit
    slm_max_new_tokens: int = field(
        default_factory=lambda: int(_get("SLM_MAX_NEW_TOKENS", "160")))
    # Lever #2: pre-encode the static system+tools prefix once per call and reuse its
    # past_key_values so per-turn prefill is only the new tokens. Opt-out if the pod's
    # transformers build mishandles the cached cache (clean fallback = full prefill).
    slm_kv_prewarm: bool = field(default_factory=lambda: _get_bool("SLM_KV_PREWARM", True))

    # --- Telephony / audio rates ---
    sample_rate_telnyx: int = 8000     # μ-law from/to the caller
    sample_rate_internal: int = 16000  # what VAD + Whisper expect
    frame_ms: int = 20                 # Telnyx media frame size (160 bytes μ-law)

    # --- VAD / turn-taking ---
    vad_silence_threshold_ms: int = field(
        default_factory=lambda: int(_get("VAD_SILENCE_THRESHOLD_MS", "300"))
    )  # silence after speech => utterance end. 300 shaves ~100ms off every turn;
       # adaptive-per-utterance endpointing is the proper Pass-2 fix.
    vad_speech_prob_threshold: float = 0.5
    vad_min_speech_ms: int = 160          # ignore blips shorter than this
    vad_preroll_ms: int = 240             # audio kept before detected onset
    vad_window_samples: int = 512         # Silero window at 16kHz (32ms)

    # --- Barge-in / turn supersede (Pass 2 / 2.1) ---
    # Backchannel classification is CONTENT-based, not duration-based: "okay" (~800ms)
    # and "wait, stop" (~800ms) are the same length, so only the words tell them apart.
    # While Aria holds the floor:
    #   < blip_ms                  -> ignore without STT (a noise/breath blip)
    #   blip_ms .. hard_interrupt  -> transcribe + match the backchannel policy below;
    #                                 a match is ignored, otherwise it supersedes
    #   > hard_interrupt_ms        -> unambiguous real speech, supersede immediately
    #                                 (no STT wait, so long interrupts stay snappy)
    backchannel_blip_ms: int = field(
        default_factory=lambda: int(_get("BACKCHANNEL_BLIP_MS", "250"))
    )
    hard_interrupt_ms: int = field(
        default_factory=lambda: int(_get("HARD_INTERRUPT_MS", "1500"))
    )
    # Policy lists (env-overridable, comma-separated). A held-floor utterance is a
    # backchannel if its whole normalized phrase is in backchannel_phrases, OR every
    # non-filler word is in backchannel_words. Tuned, not hardcoded — adjust freely.
    backchannel_words: tuple = field(default_factory=lambda: _get_tuple(
        "BACKCHANNEL_WORDS",
        ("ok", "okay", "yeah", "yep", "yup", "yes", "sure", "right", "mhm", "mmhmm",
         "uhhuh", "gotcha", "alright", "cool", "nice", "totally", "exactly", "perfect",
         "thanks", "correct", "good", "great", "wow", "huh"),
    ))
    backchannel_phrases: tuple = field(default_factory=lambda: _get_tuple(
        "BACKCHANNEL_PHRASES",
        ("got it", "sounds good", "thank you", "no problem", "makes sense", "for sure",
         "all good", "got you", "okay cool", "yeah okay", "okay thanks", "of course",
         "that works", "fair enough", "okay great"),
    ))
    # When the caller interrupts/corrects, buffer their rapid-fire utterances and only
    # commit the turn once they've settled for this long — so "actually... no... just
    # the price" coalesces into ONE turn instead of starting+cancelling repeatedly
    # (the spiral). Clean cooperative hand-offs bypass this and commit immediately.
    coalesce_debounce_ms: int = field(
        default_factory=lambda: int(_get("COALESCE_DEBOUNCE_MS", "350"))
    )

    # --- Latency-triggered micro-ack (covers TTFT spikes) ----------------------
    # If Claude hasn't produced the first sentence within ack_delay_ms of the request
    # starting, play a short natural acknowledgement from a rotating pool to cover the
    # gap; the real answer then flows in behind it. Fires ONLY on slow turns (a naked
    # TTFT spike — measured 2.2s and 4.1s in testing): normal turns reach first audio
    # (~760-1000ms from request) before the threshold, so this does NOT add an ack to
    # every turn — that every-turn robotic-ness is exactly what retired the old single
    # FAST_ACKNOWLEDGEMENT. Rotated per call to avoid repetition. MICRO_ACK_ENABLED=0
    # disables; ACK_DELAY_MS tunes the threshold by ear.
    micro_ack_enabled: bool = field(
        default_factory=lambda: _get_bool("MICRO_ACK_ENABLED", True)
    )
    ack_delay_ms: int = field(default_factory=lambda: int(_get("ACK_DELAY_MS", "1100")))
    micro_ack_phrases: tuple = field(default_factory=lambda: _get_tuple(
        "MICRO_ACK_PHRASES",
        ("Okay.", "Sure.", "Right.", "Let me see.", "One sec.", "Mm-hm.", "Gotcha."),
    ))

    # --- STT ---
    whisper_model: str = field(default_factory=lambda: _get("WHISPER_MODEL", "small.en"))
    whisper_compute_type: str = field(default_factory=lambda: _get("WHISPER_COMPUTE", "int8"))
    whisper_device: str = field(default_factory=lambda: _get("WHISPER_DEVICE", "cpu"))
    whisper_beam_size: int = 1
    # Lever #1: transcribe the utterance incrementally WHILE the caller is still talking,
    # so the final transcript is ready at end-of-speech instead of paying ~130ms STT
    # sequentially after it. v1 overlaps timing only — it does NOT change VAD turn
    # boundaries or barge-in (keeps the "spiral" fix intact). Off by default.
    stt_streaming: bool = field(default_factory=lambda: _get_bool("STT_STREAMING", False))
    stt_partial_interval_ms: int = field(
        default_factory=lambda: int(_get("STT_PARTIAL_INTERVAL_MS", "400")))

    # --- TTS ---
    tts_engine: str = field(default_factory=lambda: _get("TTS_ENGINE", "kokoro"))  # kokoro|piper
    kokoro_voice: str = field(default_factory=lambda: _get("KOKORO_VOICE", "af_heart"))
    kokoro_lang: str = "a"  # American English
    kokoro_sample_rate: int = 24000
    piper_model_path: str = field(default_factory=lambda: _get("PIPER_MODEL_PATH", ""))

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = field(default_factory=lambda: int(_get("PORT", "8000")))

    # --- Telnyx setup ---
    agent_number: str = field(default_factory=lambda: _get("AGENT_NUMBER", "+15551234567"))
    call_control_app_name: str = "voice-agent-aria"

    # --- Google Workspace (Phase 4: CRM = Sheets, demos = Calendar) ---
    google_sa_key_path: str = field(default_factory=lambda: _get("GOOGLE_SA_KEY_PATH"))
    # Base64 of the SA key JSON. Used on hosts with no file-injection channel (the
    # golden image has no sshd) — injected as a pod env var and decoded at runtime.
    # Takes precedence over the file path when set.
    google_sa_key_b64: str = field(default_factory=lambda: _get("GOOGLE_SA_KEY_B64"))
    leads_sheet_id: str = field(default_factory=lambda: _get("LEADS_SHEET_ID"))
    leads_sheet_tab: str = field(default_factory=lambda: _get("LEADS_SHEET_TAB", "Leads"))
    demo_calendar_id: str = field(default_factory=lambda: _get("DEMO_CALENDAR_ID"))
    demo_timezone: str = field(default_factory=lambda: _get("DEMO_TIMEZONE", "America/Chicago"))

    # Who Aria represents — named as the attendee on booked demos so the lead sees a
    # real person, and used in invite descriptions. Set OPERATOR_NAME to the rep.
    operator_name: str = field(default_factory=lambda: _get("OPERATOR_NAME", "the Settl team"))
    operator_email: str = field(default_factory=lambda: _get("OPERATOR_EMAIL", "operator@example.com"))

    # --- Outbound (Phase 4 step 5: proactive dialing + voicemail) ---
    # Spoken via Telnyx TTS when AMD detects a machine: identifies Aria/Settl and
    # asks for a callback to the agent number, then we hang up.
    voicemail_text: str = field(default_factory=lambda: _get(
        "VOICEMAIL_TEXT",
        "Hi, this is Aria calling on behalf of Settl. Sorry we missed you. Please "
        "give us a call back at " + _get("AGENT_NUMBER", "+15551234567")
        + " when you get a chance. Thanks, and have a great day.",
    ))
    # Aria's opening on a live outbound answer. Has a {name} slot; identifies Aria +
    # Settl and announces recording (TCPA). pipeline.py fills {name} from the lead.
    outbound_greeting: str = field(default_factory=lambda: _get(
        "OUTBOUND_GREETING",
        "Hi, is this {name}? This is Aria calling on behalf of Settl, following up "
        "on your interest. Quick heads up, this call is recorded. Do you have a minute?",
    ))

    @property
    def media_ws_url(self) -> str:
        """wss:// URL Telnyx streams audio to (with the auth token when configured)."""
        base = self.public_url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{base}/ws/media"
        if self.media_ws_token:
            url += f"?token={self.media_ws_token}"
        return url

    @property
    def webhook_url(self) -> str:
        return f"{self.public_url}/telnyx/webhook"

    @property
    def silence_windows(self) -> int:
        win_ms = self.vad_window_samples / self.sample_rate_internal * 1000
        return max(1, round(self.vad_silence_threshold_ms / win_ms))

    @property
    def min_speech_windows(self) -> int:
        win_ms = self.vad_window_samples / self.sample_rate_internal * 1000
        return max(1, round(self.vad_min_speech_ms / win_ms))


settings = Settings()
