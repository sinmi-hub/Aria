"""Off-GPU tests for StreamingTranscriber's coverage decision — the correctness-critical
bit: reuse a streamed partial ONLY when it covered enough of the final utterance, else fall
back to a full transcribe. (The async feed/decode path is exercised live on the pod.)

    .venv/bin/python -m scripts.test_streaming_stt
"""
from __future__ import annotations

import sys

import numpy as np

from app.audio.streaming_stt import StreamingTranscriber


def _check(name: str, cond: bool) -> bool:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    return cond


def main() -> int:
    ok = True
    st = StreamingTranscriber(stt=None, coverage=0.85)

    # No partial yet -> always fall back (None).
    ok &= _check("no partial -> None", st.take_if_covers(np.zeros(16000, dtype=np.float32)) is None)

    # Simulate a completed partial covering 9000 of a 10000-sample utterance (90% > 85%).
    st._text, st._samples = "how much is the demo", 9000
    full = np.zeros(10000, dtype=np.float32)
    ok &= _check("90% coverage -> reuse partial", st.take_if_covers(full) == "how much is the demo")

    # A partial covering only 50% must NOT be reused (caller re-transcribes the full audio).
    st._text, st._samples = "how much", 5000
    ok &= _check("50% coverage -> None", st.take_if_covers(full) is None)

    # Empty text never reused even if sample count says covered.
    st._text, st._samples = "", 10000
    ok &= _check("empty text -> None", st.take_if_covers(full) is None)

    # reset clears state.
    st._text, st._samples = "x", 10000
    st.reset()
    ok &= _check("reset clears", st.take_if_covers(full) is None)

    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
