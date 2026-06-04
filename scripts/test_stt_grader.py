"""Offline test for the STT grader's WER math + normalization (no model, no audio).

Run:  .venv/bin/python -m scripts.test_stt_grader
"""
from __future__ import annotations

import sys

from scripts.stt_grader import normalize_for_wer, wer


def _check(name: str, cond: bool) -> bool:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    return cond


def main() -> None:
    ok = True

    # Normalization: case + punctuation stripped, whitespace collapsed.
    ok &= _check("normalize strips case/punct",
                 normalize_for_wer("  The Settl, DEMO! ") == "the settl demo")

    # Identical -> 0 WER.
    r = wer("the settl demo is monday", "the settl demo is monday")
    ok &= _check("identical -> wer 0", r["wer"] == 0.0 and r["hits"] == 5)

    # One substitution out of 5 words -> 0.2 (the real 'shuttle' bug).
    r = wer("how much is settl", "how much is shuttle")
    ok &= _check("one sub of 4 -> wer 0.25 + 1 substitution",
                 r["wer"] == 0.25 and r["substitutions"] == 1
                 and r["deletions"] == 0 and r["insertions"] == 0)

    # One deletion (hypothesis missing a word).
    r = wer("the demo is at noon", "the demo is noon")
    ok &= _check("one deletion -> 1 del, wer 0.2",
                 r["deletions"] == 1 and r["substitutions"] == 0
                 and r["insertions"] == 0 and r["wer"] == 0.2)

    # One insertion (hypothesis has an extra word).
    r = wer("see you monday", "see you next monday")
    ok &= _check("one insertion -> 1 ins, wer ~0.33",
                 r["insertions"] == 1 and r["substitutions"] == 0
                 and r["deletions"] == 0 and round(r["wer"], 2) == 0.33)

    # Empty reference, non-empty hypothesis -> all insertions, wer 1.0 (guarded /0).
    r = wer("", "hello there")
    ok &= _check("empty ref -> wer 1.0, no crash", r["wer"] == 1.0)

    # Empty both -> 0.
    ok &= _check("empty both -> wer 0", wer("", "")["wer"] == 0.0)

    print("\nALL PASS" if ok else "\nFAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
