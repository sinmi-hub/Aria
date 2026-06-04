"""CRM adapter — leads live in a Google Sheet.

`LeadStore` is the swappable interface (a future `SettlLeadStore` implements the
same four-plus methods with zero brain/pipeline changes). `SheetsLeadStore` is the
concrete Google Sheets implementation.

Column contract (locked, columns A..J in order). The first column header is blank
in the operator's sheet but holds the contact name, so the contract is positional:

    name | phone | company | status | last_contacted | meeting_time |
    meeting_event_id | notes | recording_url | dnc
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from config import settings

from app.integrations.google_client import normalize_phone, service
from app.log import debug, warn

# Positional column contract (A..J). Index = column offset.
COLUMNS = [
    "name", "phone", "company", "status", "last_contacted", "meeting_time",
    "meeting_event_id", "notes", "recording_url", "dnc",
]
_COL_LETTER = {name: chr(ord("A") + i) for i, name in enumerate(COLUMNS)}


@dataclass
class Lead:
    name: str = ""
    phone: str = ""
    company: str = ""
    status: str = ""
    last_contacted: str = ""
    meeting_time: str = ""
    meeting_event_id: str = ""
    notes: str = ""
    recording_url: str = ""
    dnc: bool = False
    row: int = 0  # 1-based sheet row (0 = not persisted)
    extra: dict = field(default_factory=dict)

    @property
    def is_dnc(self) -> bool:
        return self.dnc


class LeadStore(ABC):
    """The swappable CRM interface Aria's tools call."""

    @abstractmethod
    def get_lead_by_phone(self, phone: str) -> Lead | None: ...

    @abstractmethod
    def get_lead_by_name(self, name: str) -> Lead | None: ...

    @abstractmethod
    def log_call(self, lead: Lead, summary: str, status: str | None = None,
                 recording_url: str | None = None, when: str | None = None) -> None: ...

    @abstractmethod
    def set_meeting_ref(self, lead: Lead, meeting_time: str, event_id: str) -> None: ...

    @abstractmethod
    def set_recording_url(self, lead: Lead, url: str) -> None: ...

    @abstractmethod
    def mark_dnc(self, lead: Lead) -> None: ...


def _to_lead(values: list[str], row: int) -> Lead:
    padded = (values + [""] * len(COLUMNS))[: len(COLUMNS)]
    data = dict(zip(COLUMNS, padded))
    return Lead(
        name=data["name"].strip(),
        phone=normalize_phone(data["phone"]),
        company=data["company"].strip(),
        status=data["status"].strip(),
        last_contacted=data["last_contacted"].strip(),
        meeting_time=data["meeting_time"].strip(),
        meeting_event_id=data["meeting_event_id"].strip(),
        notes=data["notes"].strip(),
        recording_url=data["recording_url"].strip(),
        dnc=data["dnc"].strip().upper() in {"TRUE", "YES", "1"},
        row=row,
    )


class SheetsLeadStore(LeadStore):
    def __init__(self, sheet_id: str | None = None, tab: str | None = None) -> None:
        self.sheet_id = sheet_id or settings.leads_sheet_id
        self.tab = tab or settings.leads_sheet_tab
        if not self.sheet_id:
            raise RuntimeError("LEADS_SHEET_ID is not set.")

    @property
    def _values(self):
        return service("sheets", "v4").spreadsheets().values()

    # --- reads --------------------------------------------------------------
    def _all_rows(self) -> list[Lead]:
        rng = f"{self.tab}!A2:{_COL_LETTER['dnc']}"
        rows = self._values.get(spreadsheetId=self.sheet_id, range=rng).execute().get("values", [])
        return [_to_lead(r, i + 2) for i, r in enumerate(rows) if any(c.strip() for c in r)]

    def get_lead_by_phone(self, phone: str) -> Lead | None:
        target = normalize_phone(phone)
        for lead in self._all_rows():
            if lead.phone == target:
                return lead
        return None

    def get_lead_by_name(self, name: str) -> Lead | None:
        target = name.strip().lower()
        if not target:
            return None
        leads = self._all_rows()
        for lead in leads:  # exact
            if lead.name.lower() == target:
                return lead
        for lead in leads:  # substring either way ("sinmi" ~ "sinmi ojeyomi")
            ln = lead.name.lower()
            if ln and (target in ln or ln in target):
                return lead
        for lead in leads:  # first-name match
            if lead.name and lead.name.lower().split()[0] == target.split()[0]:
                return lead
        return None

    def list_leads(self) -> list[Lead]:
        return self._all_rows()

    # --- writes -------------------------------------------------------------
    def _set_cells(self, row: int, updates: dict[str, str]) -> None:
        if not row:
            raise ValueError("Cannot write a lead with no sheet row.")
        data = [
            {"range": f"{self.tab}!{_COL_LETTER[col]}{row}", "values": [[val]]}
            for col, val in updates.items()
        ]
        self._values.batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
        debug(f"[crm] row {row} updated: {list(updates)}")

    def log_call(self, lead: Lead, summary: str, status: str | None = None,
                 recording_url: str | None = None, when: str | None = None) -> None:
        stamp = when or _today()
        note = f"{stamp}: {summary}".strip()
        merged = f"{lead.notes}\n{note}".strip() if lead.notes else note
        updates = {"last_contacted": stamp, "notes": merged}
        if status:
            updates["status"] = status
        if recording_url:
            updates["recording_url"] = recording_url
        self._set_cells(lead.row, updates)
        lead.notes, lead.last_contacted = merged, stamp
        if status:
            lead.status = status
        if recording_url:
            lead.recording_url = recording_url

    def set_meeting_ref(self, lead: Lead, meeting_time: str, event_id: str) -> None:
        self._set_cells(lead.row, {
            "meeting_time": meeting_time,
            "meeting_event_id": event_id,
            "status": "demo_booked",
        })
        lead.meeting_time, lead.meeting_event_id, lead.status = meeting_time, event_id, "demo_booked"

    def set_recording_url(self, lead: Lead, url: str) -> None:
        self._set_cells(lead.row, {"recording_url": url})
        lead.recording_url = url

    def mark_dnc(self, lead: Lead) -> None:
        self._set_cells(lead.row, {"dnc": "TRUE", "status": "dnc"})
        lead.dnc, lead.status = True, "dnc"
        warn(f"[crm] lead {lead.name or lead.phone} marked DNC")


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
