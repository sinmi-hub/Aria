# Aria — deferred / future-polish TODO

Parked items we deliberately chose **not** to do now. Pick these up later.

## ★ PRIORITY: Domain-wide delegation — unlocks attendee invites AND branding
**Status:** deferred but high priority (do before going to real prospect calls).
**One setup solves two things at once:**

1. **Attendee invites (the functional gap).** Right now `book_meeting` adds NO
   attendees — service accounts can't invite without delegation
   (`forbiddenForServiceAccounts`). So the lead (and operator) get no invite email;
   the demo only appears on the shared `Settl Demos` calendar (Layer-1 visibility).
   With DWD, Aria can invite the lead + operator → real calendar invites go out.
2. **Branded organizer (the polish).** Invites come **from `operator@example.com`**
   as organizer — no service-account / robot calendar name on the lead's invite.

**How (Google domain-wide delegation):** the `voice-agent@…` service account
impersonates `operator@example.com` when creating Calendar events.
- One-time **Workspace admin** authorization: Admin console → Security → API controls
  → Domain-wide delegation → add the SA client ID with scope
  `https://www.googleapis.com/auth/calendar`.
- In code: build the Calendar credentials with `.with_subject("operator@example.com")`
  (Sheets can stay non-delegated). Then re-enable `attendees=[lead_email, operator]`
  and `send_updates="all"` in `tools._t_book_meeting` (currently disabled with a
  pointer to this TODO).
- Needs a **lead email column** in the Sheet to invite the lead (today we only have
  phone). Add `email` to the layout when we do this.

**Trigger to revisit:** before the first batch of real prospect calls.

---

## (add future deferrals below)

---

## Recording URLs expire (Telnyx-hosted presigned links)
**Status:** works for the pilot; not durable.
**Finding (verified 2026-05-31):** `call.recording.saved` gives a presigned S3 URL with
`X-Amz-Expires=600` — it dies ~10 min after the call. We store it in the Sheet's
`recording_url`, so the stored link goes stale fast.
**Durable fix (deferred, = the "S3 ownership" item):** within the window either
(a) download the mp3 to our own S3 bucket and store that URL, or (b) store the Telnyx
recording **id** and re-fetch a fresh URL on demand via the Telnyx recordings API.
**Trigger:** before real prospect calls whose recordings need to be replayed later.

---

## Smart voicemail on outbound (AMD) — parked
**Status:** dropped for the pilot (was causing the bug below).
**What happened (2026-05-31):** outbound gated Aria's first words on Telnyx
answering-machine detection (`detect_beep`). A human pickup heard several seconds of
dead silence while AMD listened for a beep, and hung up before Aria spoke. Fix shipped:
**greet immediately on answer** (both directions); AMD removed from the dial.
**Consequence:** no machine-vs-human branching now. If a machine answers, Aria's spoken
opening (identifies Settl + context) serves as the message — good enough for the pilot.
**Proper fix later:** run AMD in PARALLEL (don't gate the greeting on it); only if it
returns "machine" do we switch to the scripted Telnyx-speak voicemail. The
`commands.speak` + `settings.voicemail_text` pieces are still in place for this.
