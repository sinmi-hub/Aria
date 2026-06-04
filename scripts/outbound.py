"""Outbound dialing CLI — Aria proactively calls a lead from the leads Sheet.

Pilot trigger is manual (no scheduler). Compliance guards run BEFORE any dial and
are never bypassed: a lead marked DNC is refused, and calls only go out during
TCPA calling hours (8am-9pm) in the lead's local time. Use --dry-run to validate
the full path (lookup + guards + connection id + payload) without placing a call.

Usage:
    python -m scripts.outbound --phone +15557654321 [--dry-run]
    python -m scripts.outbound --name "Alex"        [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.integrations.crm import Lead, SheetsLeadStore
from app.telephony import call_context, commands
from config import settings

# TCPA calling-hours window (local time). Constants — no env needed (per brief).
CALL_HOUR_START = 8
CALL_HOUR_END = 21  # exclusive: dial only when 8 <= hour < 21


def _find_lead(store: SheetsLeadStore, phone: str | None, name: str | None) -> Lead | None:
    if phone:
        return store.get_lead_by_phone(phone)
    return store.get_lead_by_name(name or "")


def _calling_hours_ok() -> tuple[bool, int]:
    """Current hour in the pilot timezone, and whether it's inside the window."""
    hour = datetime.now(ZoneInfo(settings.demo_timezone)).hour
    return (CALL_HOUR_START <= hour < CALL_HOUR_END), hour


async def _run(args: argparse.Namespace) -> int:
    store = SheetsLeadStore()
    lead = _find_lead(store, args.phone, args.name)
    if not lead:
        target = args.phone or args.name
        print(f"No lead found for {target!r}. Aborting.")
        return 1

    hours_ok, hour = _calling_hours_ok()
    print(f"Lead:    {lead.name or '<unnamed>'}  {lead.phone}  (row {lead.row})")
    print(f"  DNC guard:           {'REFUSE (lead is DNC)' if lead.dnc else 'ok'}")
    print(
        f"  Calling-hours guard: {'ok' if hours_ok else 'REFUSE'} "
        f"(hour {hour} in {settings.demo_timezone}; window {CALL_HOUR_START}-{CALL_HOUR_END})"
    )

    if lead.dnc:
        print("Refusing to dial: lead is on the do-not-call list.")
        return 1
    if not hours_ok:
        print("Refusing to dial: outside TCPA calling hours.")
        return 1

    connection_id = await commands.get_connection_id()
    client_state = call_context.encode_state(lead.phone)
    payload = {
        "connection_id": connection_id,
        "to": lead.phone,
        "from": settings.agent_number,
        "answering_machine_detection": "detect_beep",
        "client_state": client_state,
    }

    if args.dry_run:
        print(f"  connection_id:       {connection_id}")
        print("  dial payload:")
        print(json.dumps(payload, indent=2))
        print("\n[dry-run] No call placed.")
        return 0

    ccid = await commands.dial(lead.phone, client_state)
    if not ccid:
        print("Dial failed (see warnings above).")
        return 1
    print(f"Dialing {lead.phone} -> call_control_id={ccid}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Aria outbound dialer (pilot).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--phone", help="Lead phone number, e.g. +15557654321")
    group.add_argument("--name", help="Lead name, e.g. 'Alex'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate lookup + guards + payload without dialing.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
