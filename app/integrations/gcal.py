"""Calendar adapter — demo meetings live in a Google Calendar.

`CalendarStore` is the swappable interface; `GoogleCalendar` is the concrete
implementation against the shared `Settl-Agent-Meetings` calendar. All times are
handled in the calendar's local timezone (config ``DEMO_TIMEZONE``) since that is
how Aria talks about slots out loud ("Tuesday at 2").
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings

from app.integrations.google_client import service
from app.log import debug


@dataclass
class Slot:
    start: datetime
    end: datetime

    def label(self) -> str:
        # Spoken-friendly: "Tuesday, June 3 at 2:00 PM"
        return self.start.strftime("%A, %B %-d at %-I:%M %p")

    def iso(self) -> str:
        return self.start.isoformat()


class CalendarStore(ABC):
    @abstractmethod
    def find_open_slots(self, days_ahead: int = 5, duration_min: int = 30) -> list[Slot]: ...

    @abstractmethod
    def book_meeting(self, start_iso: str, summary: str, description: str = "",
                     duration_min: int = 30, attendee_email: str | None = None) -> str: ...

    @abstractmethod
    def reschedule_meeting(self, event_id: str, new_start_iso: str,
                           duration_min: int = 30) -> str: ...

    @abstractmethod
    def cancel_meeting(self, event_id: str) -> None: ...


class GoogleCalendar(CalendarStore):
    def __init__(self, calendar_id: str | None = None, tz: str | None = None) -> None:
        self.calendar_id = calendar_id or settings.demo_calendar_id
        self.tz = ZoneInfo(tz or settings.demo_timezone)
        if not self.calendar_id:
            raise RuntimeError("DEMO_CALENDAR_ID is not set.")

    @property
    def _events(self):
        return service("calendar", "v3").events()

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    # --- availability -------------------------------------------------------
    def find_open_slots(self, days_ahead: int = 5, duration_min: int = 30,
                        work_start: int = 9, work_end: int = 17,
                        max_slots: int = 6) -> list[Slot]:
        """Walk the next `days_ahead` business days in `duration_min` steps and
        return slots that don't collide with a busy block (via freebusy)."""
        now = self._now()
        window_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        window_end = (now + timedelta(days=days_ahead)).replace(
            hour=work_end, minute=0, second=0, microsecond=0)

        fb = service("calendar", "v3").freebusy().query(body={
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "timeZone": str(self.tz),
            "items": [{"id": self.calendar_id}],
        }).execute()
        busy = [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in fb["calendars"][self.calendar_id]["busy"]
        ]

        slots: list[Slot] = []
        cursor = max(window_start, now + timedelta(minutes=15))
        step = timedelta(minutes=duration_min)
        while cursor < window_end and len(slots) < max_slots:
            in_hours = work_start <= cursor.hour < work_end
            weekday = cursor.weekday() < 5
            end = cursor + step
            clashes = any(s < end and cursor < e for s, e in busy)
            if in_hours and weekday and not clashes:
                slots.append(Slot(cursor, end))
                cursor = end
            else:
                cursor += step
                if cursor.hour >= work_end:  # jump to next day's work start
                    cursor = (cursor + timedelta(days=1)).replace(
                        hour=work_start, minute=0, second=0, microsecond=0)
        debug(f"[cal] {len(slots)} open slots in next {days_ahead}d")
        return slots

    # --- mutations ----------------------------------------------------------
    def _parse(self, iso: str) -> datetime:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt

    def book_meeting(self, start_iso: str, summary: str, description: str = "",
                     duration_min: int = 30, attendees: list[str] | None = None,
                     send_updates: str = "all") -> str:
        """Create a demo event. `attendees` (lead + operator emails) get an invite
        email when send_updates='all' (Layer-2 visibility); the event also lands on
        any account the calendar is shared with (Layer-1, always works)."""
        start = self._parse(start_iso)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": str(self.tz)},
            "end": {"dateTime": (start + timedelta(minutes=duration_min)).isoformat(),
                    "timeZone": str(self.tz)},
        }
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees if e]
        event = self._events.insert(
            calendarId=self.calendar_id, body=body, sendUpdates=send_updates).execute()
        debug(f"[cal] booked {event['id']} @ {start_iso}")
        return event["id"]

    def reschedule_meeting(self, event_id: str, new_start_iso: str,
                           duration_min: int = 30, send_updates: str = "all") -> str:
        start = self._parse(new_start_iso)
        self._events.patch(calendarId=self.calendar_id, eventId=event_id, sendUpdates=send_updates,
                           body={
            "start": {"dateTime": start.isoformat(), "timeZone": str(self.tz)},
            "end": {"dateTime": (start + timedelta(minutes=duration_min)).isoformat(),
                    "timeZone": str(self.tz)},
        }).execute()
        debug(f"[cal] rescheduled {event_id} -> {new_start_iso}")
        return event_id

    def cancel_meeting(self, event_id: str, send_updates: str = "all") -> None:
        self._events.delete(
            calendarId=self.calendar_id, eventId=event_id, sendUpdates=send_updates).execute()
        debug(f"[cal] cancelled {event_id}")
