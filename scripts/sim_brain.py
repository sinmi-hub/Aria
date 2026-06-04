"""Text-level test of Aria's brain + tool-use loop against the REAL Google adapters.

Drives a scripted multi-turn conversation through brain.stream_reply with a real
AriaTools (Sheets + Calendar), printing Aria's spoken text and (with DEBUG=1) the
tool calls. Proves lookup -> answer -> find slots -> book -> log without a server,
phone, or audio. Cleans up after itself: cancels any event booked and restores the
test lead's sheet row.

Usage:  DEBUG=1 .venv/bin/python -m scripts.sim_brain
"""
from __future__ import annotations

import asyncio

from app.agent import brain
from app.agent.tools import AriaTools
from app.integrations.crm import COLUMNS, SheetsLeadStore

TEST_PHONE = "4439292703"  # the operator's own test-lead row

SCRIPT = [
    "Hi, I think I missed a call from this number about some moving software?",
    "What does Settl actually do for a moving company?",
    "Okay, I'd like to see it. Can we set up a demo this week?",
    "Let's do the earliest one you mentioned.",
    "Great, that's all for now, thanks.",
]


async def say(history: list[dict], tools: AriaTools, text: str) -> None:
    history.append({"role": "user", "content": text})
    print(f"\nCALLER: {text}")
    reply = ""
    async for delta in brain.stream_reply(history, tools):
        reply += delta
    print(f"ARIA  : {reply.strip()}")
    history.append({"role": "assistant", "content": reply.strip()})


async def main() -> None:
    store = SheetsLeadStore()
    # snapshot the test row so we can restore it
    row_before = store._values.get(
        spreadsheetId=store.sheet_id,
        range=f"{store.tab}!A2:{chr(ord('A')+len(COLUMNS)-1)}2",
    ).execute().get("values", [["" ] * len(COLUMNS)])[0]
    print(f"[setup] snapshot row 2: {row_before}")

    tools = AriaTools(store=store, caller_phone=TEST_PHONE)
    history: list[dict] = []
    try:
        for line in SCRIPT:
            await say(history, tools, line)
    finally:
        # cleanup: cancel any booked event + restore the row exactly
        if tools.lead and tools.lead.meeting_event_id:
            try:
                tools.calendar.cancel_meeting(tools.lead.meeting_event_id, send_updates="none")
                print(f"\n[cleanup] cancelled event {tools.lead.meeting_event_id[:12]}…")
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] event cancel failed: {exc}")
        padded = (row_before + [""] * len(COLUMNS))[: len(COLUMNS)]
        store._set_cells(2, dict(zip(COLUMNS, padded)))
        print("[cleanup] restored row 2 to its original values")


if __name__ == "__main__":
    asyncio.run(main())
