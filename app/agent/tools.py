"""Aria's tool set — the bridge between Claude's tool-use and the Google adapters.

`AriaTools` is created once per call. It holds the call context (who Aria is
talking to) and dispatches Claude's tool calls to the CRM (Sheets) and Calendar
adapters. `SCHEMAS` is the Anthropic tool-definition list handed to the model.

The adapters are synchronous (googleapiclient), so every dispatch runs them in a
thread to keep the asyncio audio loop responsive.
"""
from __future__ import annotations

import asyncio
import time

from config import settings

from app import trace
from app.integrations.crm import Lead, LeadStore, SheetsLeadStore
from app.integrations.gcal import CalendarStore, GoogleCalendar
from app.integrations.google_client import normalize_phone
from app.log import debug

SCHEMAS = [
    {
        "name": "lookup_lead",
        "description": "Look up the person on the call in the leads list, by phone "
                       "number or name. Use this first to learn who you're talking to, "
                       "their company, status, and any booked demo. Inbound calls: try "
                       "the caller's phone first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Phone number, any format."},
                "name": {"type": "string", "description": "Full name of the contact."},
            },
        },
    },
    {
        "name": "find_open_slots",
        "description": "Get real open demo slots on the calendar to offer the caller. "
                       "Returns numbered options with a spoken label and an iso value. "
                       "Pass the iso of the chosen one to book_meeting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "How many days out to "
                               "search (default 5)."},
            },
        },
    },
    {
        "name": "book_meeting",
        "description": "Book a Settl demo for the current lead at one of the slots from "
                       "find_open_slots. Requires a lead looked up first. Confirm the time "
                       "with the caller out loud before calling this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slot_number": {"type": "integer", "description": "Which slot from the most "
                                "recent find_open_slots list (1, 2, 3, ...)."},
            },
            "required": ["slot_number"],
        },
    },
    {
        "name": "reschedule_meeting",
        "description": "Move the current lead's existing demo to one of the slots from "
                       "find_open_slots. Requires a lead with a booked meeting and a recent "
                       "find_open_slots call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slot_number": {"type": "integer", "description": "Which slot from the most "
                                "recent find_open_slots list."},
            },
            "required": ["slot_number"],
        },
    },
    {
        "name": "log_call",
        "description": "Record what was discussed on this call to the lead's row. Always "
                       "call this before the call ends, even for a no-answer or voicemail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "1-2 sentence summary of the "
                            "conversation and outcome."},
                "status": {"type": "string", "description": "Optional new status: "
                           "contacted, demo_booked, customer, no_answer."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "flag_for_human",
        "description": "Escalate to a human rep — for hot leads, pricing negotiations, or "
                       "anything outside your scope. Logs the flag on the lead's row.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why a human should follow up."},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "mark_do_not_call",
        "description": "If the caller asks to stop being contacted, mark them do-not-call. "
                       "Never dial or follow up after this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
        },
    },
]


class AriaTools:
    def __init__(self, store: LeadStore | None = None, calendar: CalendarStore | None = None,
                 caller_phone: str | None = None) -> None:
        self.store = store or SheetsLeadStore()
        self.calendar = calendar or GoogleCalendar()
        self.caller_phone = caller_phone
        self.outbound = False  # set by the pipeline once direction is resolved
        self.lead: Lead | None = None
        self._lead_phone: str | None = None  # normalized phone of the cached lead
        self._slots: list = []  # last find_open_slots result; booked by number
        self._call_logged = False  # log_call fires once; closing pleasantries re-trigger it

    @property
    def schemas(self) -> list[dict]:
        return SCHEMAS

    def call_context(self) -> str:
        """A line appended to the system prompt telling Aria who is on the line and how
        to pitch the call. Direction matters: an inbound caller wants speed; an outbound
        follow-up is an interruption Aria has to earn, so she warms up a touch."""
        if self.outbound and self.caller_phone:
            return (
                "\n\nCALL CONTEXT: This is an outbound follow-up call YOU placed to "
                f"{normalize_phone(self.caller_phone)}. They didn't ask to be called, so "
                "open warm and earn the minute — a sentence or two is welcome here, don't "
                "be clipped. You've already greeted them. Use lookup_lead with this number "
                "to pull their details."
            )
        if self.caller_phone:
            return (
                "\n\nCALL CONTEXT: This is an inbound call — they called you with something "
                "in mind, so be quick and direct. The caller's phone number is "
                f"{normalize_phone(self.caller_phone)}. Call lookup_lead with this phone "
                "number right away to identify them — do not ask for their name first."
            )
        return ""

    async def dispatch(self, name: str, args: dict) -> str:
        """Run a tool by name; return a plain-text result for the tool_result block."""
        debug(f"[tools] -> {name}({args})")
        t0 = time.perf_counter()
        try:
            handler = getattr(self, f"_t_{name}", None)
            if handler is None:
                trace.tool_call(name, args, "(unknown tool)", 1000 * (time.perf_counter() - t0))
                return f"Unknown tool {name!r}."
            result = await asyncio.to_thread(handler, args)
            ms = 1000 * (time.perf_counter() - t0)
            trace.tool_call(name, args, result, ms)
            debug(f"[tools] <- {name}: {result[:120]} ({ms:.0f}ms)")
            return result
        except Exception as exc:  # noqa: BLE001 — tool failures must not crash the call
            trace.tool_call(name, args, f"FAILED: {exc!r}", 1000 * (time.perf_counter() - t0))
            trace.error(f"tool:{name}", exc)
            return f"That action failed: {exc}. Tell the caller you'll have someone follow up."

    # --- handlers (sync; run in a thread) -----------------------------------
    def _t_lookup_lead(self, args: dict) -> str:
        phone = args.get("phone") or self.caller_phone
        # Cache hit: same phone we already resolved this call. A barge-in cancels and
        # restarts the turn, and Claude re-fires lookup_lead — without this we re-hit
        # Sheets several times for the same person (seen 4x in one call).
        if self.lead and phone and normalize_phone(phone) == self._lead_phone:
            debug("[tools] lookup_lead cache hit")
            return ("You already have this caller identified — do NOT call lookup_lead "
                    "again this call. " + self._lead_summary(self.lead))
        lead = None
        if phone:
            lead = self.store.get_lead_by_phone(phone)
        if not lead and args.get("name"):
            lead = self.store.get_lead_by_name(args["name"])
        if not lead:
            return "No matching lead found. Treat them as a new prospect; ask for their " \
                   "name and company so the call can be logged."
        self.lead = lead
        self._lead_phone = normalize_phone(lead.phone) if lead.phone else (
            normalize_phone(phone) if phone else None)
        return self._lead_summary(lead)

    @staticmethod
    def _lead_summary(lead: Lead) -> str:
        booked = (f" They have a demo booked for {lead.meeting_time}."
                  if lead.meeting_time else " No demo booked yet.")
        dnc = " THIS LEAD IS DO-NOT-CALL." if lead.is_dnc else ""
        return (f"Lead: {lead.name or 'unknown'} at {lead.company or 'unknown company'}. "
                f"Status: {lead.status or 'new'}. Last notes: {lead.notes or 'none'}.{booked}{dnc}")

    def _t_find_open_slots(self, args: dict) -> str:
        days = int(args.get("days_ahead", 5))
        self._slots = self.calendar.find_open_slots(days_ahead=days)
        if not self._slots:
            return "No open slots in that window. Offer to widen the search."
        lines = [f"{i+1}. {s.label()}" for i, s in enumerate(self._slots)]
        return "Open slots (refer to the caller by time, book by number):\n" + "\n".join(lines)

    def _require_lead(self) -> Lead:
        if not self.lead:
            raise RuntimeError("no lead in context — look the lead up first")
        return self.lead

    def _pick_slot(self, args: dict):
        if not self._slots:
            raise RuntimeError("call find_open_slots first")
        n = int(args["slot_number"])
        if not 1 <= n <= len(self._slots):
            raise RuntimeError(f"slot {n} is out of range (1-{len(self._slots)})")
        return self._slots[n - 1]

    def _t_book_meeting(self, args: dict) -> str:
        lead = self._require_lead()
        slot = self._pick_slot(args)
        summary = f"Settl demo — {lead.company or lead.name or 'prospect'}"
        desc = (f"{settings.operator_name} from Settl will walk "
                f"{lead.name or 'the prospect'} through the platform. Booked by Aria. "
                f"Lead phone: {lead.phone}.")
        # No attendees in the pilot: service accounts can't invite without domain-wide
        # delegation (see TODO.md). The event lands on the shared 'Settl Demos' calendar,
        # so the operator sees every booking.
        event_id = self.calendar.book_meeting(slot.iso(), summary, desc, send_updates="none")
        self.store.set_meeting_ref(lead, slot.iso(), event_id)
        return f"Booked the demo for {lead.company or lead.name} on {slot.label()}. " \
               f"It's on the Settl Demos calendar."

    def _t_reschedule_meeting(self, args: dict) -> str:
        lead = self._require_lead()
        if not lead.meeting_event_id:
            return "This lead has no booked demo to move. Offer to book a new one with " \
                   "find_open_slots."
        slot = self._pick_slot(args)
        self.calendar.reschedule_meeting(lead.meeting_event_id, slot.iso(), send_updates="none")
        self.store.set_meeting_ref(lead, slot.iso(), lead.meeting_event_id)
        return f"Moved the demo to {slot.label()}."

    def _t_log_call(self, args: dict) -> str:
        # Once per call. Claude tends to re-log on every closing "thanks/bye", which
        # re-writes the CRM row and spends an extra tool round each time. The first
        # summary is already complete, so later calls are a no-op acknowledgement.
        if self._call_logged:
            return "Already logged this call — do not call log_call again."
        lead = self._require_lead()
        self.store.log_call(lead, args["summary"], status=args.get("status"))
        self._call_logged = True
        return "Logged to the lead's row."

    def _t_flag_for_human(self, args: dict) -> str:
        lead = self._require_lead()
        self.store.log_call(lead, f"[FLAGGED FOR HUMAN] {args['reason']}", status="contacted")
        return "Flagged for a human rep to follow up."

    def _t_mark_do_not_call(self, args: dict) -> str:
        lead = self._require_lead()
        self.store.mark_dnc(lead)
        return "Marked do-not-call. They won't be contacted again."
