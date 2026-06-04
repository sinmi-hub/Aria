# Implementation brief — Phase 4 / step 5: outbound dialing + compliance

Hand this to a coding agent working in `~/voice-agent`. Build **outbound calling**:
Aria proactively calls a lead from the leads Sheet, opens with context, and on
no-answer/voicemail leaves a short scripted message. Inbound already works — reuse it.

## Locked decisions
- **No-answer → leave a short voicemail** (Telnyx TTS `speak`, then hang up), don't just retry.
- **Trigger → manual CLI** for the pilot (`scripts/outbound.py`). No scheduler.
- **Recording storage = Telnyx-hosted** (same as inbound; recording already works).

## Read first (match these patterns exactly)
- `app/telephony/commands.py` — thin async Telnyx actions (`answer`, `streaming_start`,
  `record_start`, `hangup`, `_action`). All POST to `/v2/calls/{ccid}/actions/{action}`.
- `scripts/setup_telnyx.py` — `upsert_app()` shows how to find the Call Control
  application by `settings.call_control_app_name` ("voice-agent-aria"); its **id is the
  `connection_id`** you need to dial.
- `app/telephony/webhook.py` — event dispatch; inbound = answer → streaming_start +
  record_start. `call_context.py` — ccid→caller-phone map (already used for lookup).
- `app/agent/pipeline.py` — `CallPipeline(session, caller_phone=...)`, `start_greeting()`
  speaks `AGENT_GREETING`. `app/integrations/crm.py` — `SheetsLeadStore` (`get_lead_by_phone`,
  `get_lead_by_name`, `log_call`, `mark_dnc`, `list_leads`, `Lead.dnc`).

## What to build

### 1. `commands.py` — dial + speak
- `async def dial(to: str, client_state_b64: str) -> str | None`: POST **`/v2/calls`**
  (note: NOT an `/actions/` sub-path — it's the create-call endpoint) with body:
  `{ "connection_id": <app id>, "to": to, "from": settings.agent_number,
     "answering_machine_detection": "detect_beep",
     "client_state": client_state_b64 }`. Return the new `call_control_id` from the
  response (`data.call_control_id`), or None on error (log it).
- `async def get_connection_id() -> str`: GET `/v2/call_control_applications`, find the
  app whose `application_name == settings.call_control_app_name`, return its `id`. Cache
  it in a module global so we don't refetch every dial.
- `async def speak(ccid: str, text: str) -> None`: POST `/v2/calls/{ccid}/actions/speak`
  with `{ "payload": text, "voice": "female", "language": "en-US" }` (Telnyx TTS — fine
  for a voicemail; we don't need Kokoro here).

### 2. `client_state` helpers (put in `call_context.py`)
Telnyx echoes `client_state` (base64) back on every event for the call. Use it to carry
the lead's phone + an outbound marker:
- `encode_state(phone: str) -> str`: base64 of `json.dumps({"outbound": True, "phone": phone})`.
- `decode_state(b64: str) -> dict`: reverse; return `{}` on any error.

### 3. `webhook.py` — outbound branch + AMD + voicemail
The payload has `direction` and `client_state`. Branch:
- **`call.initiated` with `direction == "outgoing"`**: decode `client_state` → if outbound,
  `call_context.remember(ccid, state["phone"])` AND mark it outbound (extend call_context to
  store an `outbound` flag per ccid — e.g. a second dict, or store a small dict as the value).
  Do NOT answer (we placed the call; Telnyx auto-progresses).
- **`call.answered` for an outbound call**: do **nothing yet** — wait for AMD. (Inbound
  `call.answered` keeps its current behavior: streaming_start + record_start.)
- **AMD result events** (`answering_machine_detection: "detect_beep"` emits
  `call.machine.detection.ended` with `payload.result` in {human, machine, not_sure,
  silence}, and `call.machine.greeting.ended` after the machine's greeting/beep):
  - `result == "human"` (or `not_sure`) → `streaming_start(ccid)` + `record_start(ccid)`
    (the normal talking path; the pipeline will greet with outbound context — see §4).
  - `result == "machine"` → wait for `call.machine.greeting.ended`, then
    `speak(ccid, settings.voicemail_text)`, and on the following `call.speak.ended`
    event → `hangup(ccid)`. Also `log_call` the lead as a left-voicemail outcome.
- Keep the existing `call.recording.saved` handling. Keep `call_context.forget(ccid)` on
  recording.saved (and also forget on hangup for voicemail calls that never record).

### 4. `pipeline.py` — outbound opening
`CallPipeline.__init__(self, session, caller_phone=None, outbound=False)`. In
`start_greeting()`: if `outbound` and tools are available, look up the lead
(`self.tools.store.get_lead_by_phone(self.caller_phone)`) and speak a **context opening**
built from `settings.outbound_greeting` instead of the generic `AGENT_GREETING`, e.g.:
> "Hi, is this {name}? This is Aria calling on behalf of Settl — following up on your
> interest. Quick heads up, this call is recorded. Do you have a minute?"
Fall back to a generic outbound line if no lead/name. `media_ws._begin_call` must read the
outbound flag from `call_context` and pass `outbound=` to `CallPipeline`.

### 5. `scripts/outbound.py` — the CLI (with compliance guards)
Usage: `python -m scripts.outbound --phone +1... [--dry-run]` and
`python -m scripts.outbound --name "Alex" [--dry-run]`. Steps:
1. Look up the lead in the Sheet (`SheetsLeadStore`). Bail if not found.
2. **Compliance guards (refuse to dial if any fail):**
   - **DNC**: if `lead.dnc` → refuse.
   - **Calling hours (TCPA)**: only 8am–9pm in the lead's local time. We only have one tz
     for the pilot — use `settings.demo_timezone` (America/Chicago). Compute current hour
     there; refuse if `not (8 <= hour < 21)`.
3. `--dry-run`: print the lead, the guard results, the resolved `connection_id`, and the
   dial payload — but DON'T call. (This is the safe validation path.)
4. Otherwise: `dial(lead.phone, encode_state(lead.phone))`, print the new ccid.

### 6. `config.py` additions
- `voicemail_text` (env `VOICEMAIL_TEXT`, default a short scripted VM identifying Aria/Settl,
  asking for a callback to `settings.agent_number`).
- `outbound_greeting` (env `OUTBOUND_GREETING`, default the §4 template with a `{name}` slot).
- Calling-hours bounds can be constants (8, 21) — no env needed.

## Compliance notes (must be honored)
- Outbound opening identifies Aria + Settl and **announces recording** (it's in the §4
  template — keep it).
- Calling-hours + DNC guards live in the CLI (§5) — never bypass them.
- If a caller says stop/don't-call mid-conversation, the existing `mark_do_not_call` tool
  handles it (no change needed).

## Constraints
- **May edit:** `app/telephony/commands.py`, `app/telephony/webhook.py`,
  `app/telephony/call_context.py`, `app/agent/pipeline.py`, `config.py`, and NEW
  `scripts/outbound.py`. Do NOT touch `deploy/`, the Dockerfile, `app/audio/`,
  `app/agent/brain.py`, `app/agent/tools.py`, or `app/integrations/`.
- **No git commits/pushes.** No new dependencies. No secrets in code. Telnyx-hosted
  recording only (no S3). Keep every file under 300 lines.

## Validate before reporting done
- `python -m py_compile` every changed/new file.
- Run the CLI in **`--dry-run`** for the test lead (`--phone 4439292703`): show it resolves
  the lead, evaluates DNC + calling-hours guards, fetches a real `connection_id` from
  Telnyx, and prints a well-formed dial payload — without placing a call.
- Confirm `encode_state`/`decode_state` round-trip.
- Do NOT place a live outbound call in validation — the operator will test that on a real
  deploy. Report changes with file:line refs + the dry-run output. Do not deploy.

---

## Telnyx account prerequisite (one-time, NOT code)
Outbound dialing returns `403 D38 / 10010 "Connection has no Outbound Profile
assigned"` until the Call Control connection has an **Outbound Voice Profile**.
Inbound needs none. Created 2026-05-31: profile `aria-outbound` (US only,
$5.00/day spend cap enabled, 2 concurrent) attached to connection
2971577704907801956. Persists across deploys. A fresh Telnyx account must redo this
(POST /v2/outbound_voice_profiles, then PATCH the call control app's `outbound`).
