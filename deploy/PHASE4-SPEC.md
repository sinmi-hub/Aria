# Phase 4 Spec — "Aria", the Settl sales agent

**Status:** proposed, ready to execute.
**Prereq:** Phases 1–3 (voice pipeline on RunPod GPU, Kokoro voice, security hardening).
**One-line goal:** Aria is an AI voice agent that **sells/serves on behalf of Settl's own
sales motion** — she answers after-hours callbacks *and* makes outbound follow-up calls to
leads, **answers questions about the Settl product**, **books and reschedules demo meetings
on Google Calendar**, and **logs every call + what was discussed to the leads Google Sheet**.
She also doubles as a live selling point ("this is the kind of agent you could run").

This is written to be executed hands-off, in the same `[DECISION]`-marked style as
`SPEC.md`. It corrects the earlier mistaken target: **Aria does NOT wire into Settl's
booking API.** The user sells Settl; the CRM of record is **Google Sheets**, the calendar is
**Google Calendar**, and Settl is Aria's **knowledge domain**, not her database.

---

## 1. The corrected mental model (read this first)
- The operator **sells Settl**; they are not a moving company. Aria is their sales agent.
- **Leads live in a Google Sheet** — that is the CRM. Aria reads leads and writes back
  "called / what was discussed / outcome / recording link".
- **Meetings (demos) live in Google Calendar.** Aria creates and moves events.
- **Aria's knowledge = the Settl product** (features, pricing, value prop), injected as a
  prompt blob from `~/web-portal/docs/settl/` + the canonical thesis. Fits in a prompt —
  **no RAG / vector DB** in this phase.
- The voice pipeline (VAD→STT→Claude→Kokoro, barge-in, recording) stays on the RunPod box,
  unchanged. Phase 4 adds the **brain's job, tools, outbound dialing, recording, and the
  Google integrations**.

## 2. Locked decisions
- **Direction: inbound + outbound.** Inbound = prospects call the Telnyx number after hours.
  Outbound = Aria proactively calls leads from the Sheet.
- **Meetings: Google Calendar.**
- **CRM: Google Sheets**, behind a swappable adapter (so "dogfood Settl as CRM later" is a
  drop-in change, not a rewrite).
- **Recording: yes**, via Telnyx native recording (see §6).

## 3. Architecture (what's reused vs net-new)

```
INBOUND  prospect dials Telnyx# ── existing webhook ── answer ── streaming_start ─┐
OUTBOUND trigger (Sheet row / API) ── Telnyx dial (+AMD) ── on answer ── stream ──┤
                                                                                  ▼
        ┌──────────────── voice-agent (RunPod, REUSED pipeline) ────────────────┐
        │ VAD → faster-whisper → Claude(tool-loop) → Kokoro → μ-law → caller     │
        │                              │ tools                                    │
        └──────────────────────────────┼─────────────────────────────────────────┘
                                        ▼
        Google Sheets (leads/CRM)  ·  Google Calendar (demos)  ·  Telnyx recording → S3
```

**Reused unchanged:** the entire media pipeline, barge-in, the inbound webhook +
`streaming_start`, the security layer (`MEDIA_WS_TOKEN`, webhook signature).

**Net-new in Phase 4:**
1. **Claude tool-use loop** in `app/agent/brain.py` (today it only streams text). Aria must
   call tools mid-conversation and keep talking ("let me check the calendar… okay, I have
   Tuesday at 2"). See §5.
2. **Outbound dialing** — Telnyx Call Control `dial` + answering-machine detection (§6).
3. **Call context injection** — who Aria is calling/answering and why, loaded into the
   system prompt + opening line (via Telnyx `client_state` on outbound, or phone-number
   lookup on inbound).
4. **Google integrations** — Sheets (CRM) + Calendar, behind a `crm`/`calendar` interface.
5. **Recording** — Telnyx `record_start` → S3 → URL written to the Sheet row.
6. **Knowledge blob** — Settl product facts compiled into the system prompt.
7. **Outbound trigger** — a way to start calls (CLI for pilot; `POST /calls/outbound` later).
8. **Compliance guards** — calling hours, identification, recording consent (§7).

## 4. Integrations + dependencies (status)

| Need | Status | Action |
|---|---|---|
| Google **Sheets** read/write | ✅ creds exist: `~/.config/gsheets-credentials.json` + `gsheets-token.json` (scopes: `spreadsheets`,`drive`; "installed" OAuth) | reuse |
| Google **Calendar** | ⚠️ **scope missing** | re-run OAuth consent adding `calendar.events`; copy refreshed token to the pod. **[DECISION]/one-time** |
| Leads **Sheet ID** + column layout | ❓ operator provides | `[DECISION]`: which sheet, which columns (name, phone, status, meeting, notes, recording_url) |
| **S3 bucket** for recordings | same AWS account | pick/confirm a bucket; Telnyx external-storage creds OR pull via webhook |
| Settl **knowledge** source | ✅ `~/web-portal/docs/settl/` (Pitch-Deck, positioning, ICP, vision) + thesis | compile to a prompt blob, operator-editable |
| Telnyx / Anthropic | ✅ in `deploy/.env` | reuse |

**Headless-OAuth note:** the Google creds are a desktop ("installed") OAuth app with a stored
refresh token. On the headless pod the refresh token works fine; only the **one-time consent
for the added Calendar scope** must happen interactively on a machine with a browser, then the
token JSON is copied to the pod (mounted as a secret, not baked into the image).

## 5. The tool set (Claude tool-use loop)

Aria's tools — each a thin Python function behind a swappable adapter:

| Tool | Backend | Purpose |
|---|---|---|
| `lookup_lead(phone or name)` | Sheets | Identify the caller / pull lead context (status, history, last note). |
| `answer_about_settl(question)` | prompt blob | Answer product/pricing/value questions. Mostly handled by the system prompt; a tool only if you want retrieval over the docs later. |
| `find_open_slots(range)` | Calendar | Offer real availability. |
| `book_meeting(when, lead)` | Calendar | Create the demo event + invite the lead + the rep. |
| `reschedule_meeting(event, when)` | Calendar | Move an existing demo. |
| `log_call(lead, summary, outcome)` | Sheets | Write "called / discussed / outcome" + recording link to the row. |
| `flag_for_human(lead, reason)` | Sheets/notify | Escalate hot leads or anything out of scope to the rep. |

**Loop shape** (in `brain.py`): stream Claude; on `tool_use`, run the tool, return
`tool_result`, continue. Speak natural filler around slow tools so there's no dead air. Keep
the sentence-chunker → TTS path for everything Aria says. Always `log_call` at end of call
(even no-answer/voicemail).

## 6. Outbound dialing + recording (Telnyx)
- **Dial:** `POST /v2/calls` (Call Control) with `connection_id` (our app), `to`=lead,
  `from`=our Telnyx number, and **`answering_machine_detection`** enabled. On
  `call.answered` → existing `streaming_start`. On AMD = machine → either leave a short
  scripted voicemail (TTS then hangup) or hang up + mark for retry. **[DECISION]: leave VM?**
- **Context:** pass lead/purpose via Telnyx `client_state` (base64) on dial; echo it back in
  webhook/start → load into the prompt so Aria opens with "Hi, is this {name}? It's Aria
  following up on Settl…".
- **Recording:** `POST /v2/calls/{id}/actions/record_start` with `format=mp3`,
  `channels=dual` (caller + Aria on separate tracks). On `call.recording.saved` webhook, take
  the URL → write to the Sheet row. **Storage [DECISION]:** Telnyx-hosted vs your **S3**
  (external storage; same AWS account) — recommend S3 so you own the files.

## 7. Compliance (required, not optional)
Outbound automated sales calls have legal constraints — bake these in:
- **Calling hours:** only dial within the lead's local 8am–9pm (TCPA). Add a guard before any
  outbound dial.
- **Identification:** Aria states who she is and that she's calling on behalf of Settl.
- **Recording consent:** Aria announces "this call is recorded" in her opening (two-party
  consent states). Applies to inbound too.
- **Opt-out / DNC:** if a lead says stop/don't call, `log_call` marks DNC and never dial again.

## 8. CRM adapter (swappable — "same logic")
Define one interface, e.g. `app/integrations/crm.py: class LeadStore` with
`get_lead`, `log_call`, `set_meeting_ref`, `mark_dnc`. Ship a **`SheetsLeadStore`** now;
a future **`SettlLeadStore`** (if you dogfood Settl) implements the same interface — no
pipeline/brain changes. Same idea for `calendar.py` (Google now).

## 9. Validation / definition of done
1. **Inbound:** call the Telnyx number → Aria answers, identifies the caller from the Sheet,
   answers a real Settl question, books a demo on Calendar, and the Sheet row shows
   called + summary + recording link.
2. **Outbound:** trigger a call to your own phone (acting as the lead) → Aria opens with
   context, handles voicemail correctly if you don't pick up, logs the outcome.
3. **Recording:** the mp3 lands in S3 and its URL is on the Sheet row.
4. **Compliance:** outbound respects calling hours; recording is announced; "stop calling"
   sets DNC.
5. **Knowledge:** Aria correctly answers 5 canned Settl questions from the blob.

**First E2E test (operator-defined):** the first row in the leads Sheet is the operator
themselves, scenario = an after-hours **inbound callback** where they ask Aria questions
about Settl and (optionally) move a meeting. This is the go/no-go for the inbound path.

**Deployment posture (resolved):** stay on RunPod **Community Cloud** through this E2E test
and until the Google integrations are live; **convert to Secure Cloud** only after that, once
it carries real prospect recordings/PII and needs production uptime (flip
`runpod_up.py --cloud-type COMMUNITY` → `SECURE`; ~1.5–2× rate, golden image unchanged).

## 10. Open `[DECISION]`s for the operator
- Leads **Sheet ID** + column layout (name, phone, status, meeting ref, notes, recording_url).
- Add **Calendar scope** to the Google creds (one-time consent) — or use a separate calendar
  service account.
- **Voicemail:** leave a scripted message on no-answer, or hang up + retry?
- **Recording storage:** S3 (recommended) vs Telnyx-hosted.
- **Outbound trigger:** manual CLI for the pilot vs a scheduled "work the list" job vs a
  button. Recommend CLI first.
- **Persona/voice:** keep "Aria/af_heart", or a distinct sales voice.

## 11. Suggested build order
1. **Google adapters** — `SheetsLeadStore` (read+log) and `GoogleCalendar` (slots/book/move),
   validated standalone against a test sheet/calendar.
2. **Tool-use loop** in `brain.py` + wire the tools; prove with `sim_call` (text-level) using
   mock then real adapters.
3. **Knowledge blob** from `~/web-portal/docs/settl/` → system prompt; QA the 5 questions.
4. **Recording** via Telnyx `record_start` → S3 → Sheet.
5. **Outbound** dial + AMD + `client_state` context + compliance guards; trigger CLI.
6. **End-to-end** validation (§9) on a real inbound + a real outbound call to your phone.

The thesis still holds: open-source AI stack, only Telnyx + Claude + (now) Google Workspace as
external surfaces, on the one cheap GPU box. Aria graduates from "works and sounds great" to
"runs your after-hours sales desk."
