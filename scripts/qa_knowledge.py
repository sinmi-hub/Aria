"""QA Aria's Settl knowledge — asks the canned sales questions and prints her answers.

Knowledge-only: no tools (so no Sheet/Calendar writes), each question a fresh single
turn. Read the answers to check she leads operational, quotes pricing correctly, and
stays honest about being early.

Usage:  .venv/bin/python -m scripts.qa_knowledge
"""
from __future__ import annotations

import asyncio

from app.agent import brain

QUESTIONS = [
    "So what is Settl exactly?",
    "How much does it cost?",
    "I already use spreadsheets and texts to run my jobs. What does this do that I can't?",
    "How does the customer side work — do my customers have to use something?",
    "Does it handle deposits and getting me paid?",
    "What happens when the job changes and I need to charge more?",
]


async def ask(question: str) -> None:
    history = [{"role": "user", "content": question}]
    reply = ""
    async for delta in brain.stream_reply(history, tools=None):
        reply += delta
    print(f"\nQ: {question}\nA: {reply.strip()}")


async def main() -> None:
    for q in QUESTIONS:
        await ask(q)


if __name__ == "__main__":
    asyncio.run(main())
