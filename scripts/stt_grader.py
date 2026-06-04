"""Offline STT grader — our own thin faster-whisper wrapper (no MCP, no shell-out).

Why this exists: the audit of jwulff/whisper-mcp confirmed a command-injection RCE
(unquoted args into whisper-cli) and an abandoned project, so instead of hardening
third-party code we wrap faster-whisper directly. faster-whisper is a Python library
(CTranslate2) — we pass the audio path to a method, never to a shell — so the whole
injection class is gone by construction.

What it does:
  1. transcribe  — gold-transcribe a recording with a high-quality model (+ a Settl
                   vocabulary hint, since live STT keeps hearing "Settl" as "shuttle").
  2. wer         — word error rate between a reference and a hypothesis transcript.
  3. grade       — transcribe a recording (gold) and score our LIVE caller transcripts
                   (from /debug/turns, saved to a JSON file) against it.

Run (in the project venv, where faster-whisper is installed):
  .venv/bin/python -m scripts.stt_grader transcribe call.wav --model large-v3
  .venv/bin/python -m scripts.stt_grader wer "the settl demo" "the shuttle demo"
  .venv/bin/python -m scripts.stt_grader grade call.wav --turns turns.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# A vocabulary hint biases whisper toward our domain words. "Settl" is the big one —
# medium.en transcribes it as "shuttle" repeatedly in live calls.
DEFAULT_INITIAL_PROMPT = (
    "This is a phone call about Settl, software for moving companies that handles "
    "quoting, scheduling, dispatch, crew, and payments. The company is called Settl."
)

_NORM_RE = re.compile(r"[^a-z0-9 ]")


def normalize_for_wer(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so WER measures words, not
    casing/punctuation noise."""
    return _NORM_RE.sub("", (text or "").lower()).strip()


def wer(reference: str, hypothesis: str) -> dict:
    """Word error rate via word-level edit distance with operation counts.

    WER = (substitutions + deletions + insertions) / reference_word_count.
    Returns the rate plus the S/D/I/hits breakdown so a bad number is diagnosable.
    """
    ref = normalize_for_wer(reference).split()
    hyp = normalize_for_wer(hypothesis).split()
    n, m = len(ref), len(hyp)
    # cost[i][j] = min edits to turn ref[:i] into hyp[:j]
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
    for j in range(1, m + 1):
        cost[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                cost[i][j] = cost[i - 1][j - 1]
            else:
                cost[i][j] = 1 + min(cost[i - 1][j - 1],  # substitute
                                     cost[i - 1][j],      # delete
                                     cost[i][j - 1])      # insert
    # Backtrace for S/D/I/hits.
    i, j = n, m
    sub = dele = ins = hits = 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] and cost[i][j] == cost[i - 1][j - 1]:
            hits += 1; i -= 1; j -= 1
        elif i > 0 and j > 0 and cost[i][j] == cost[i - 1][j - 1] + 1:
            sub += 1; i -= 1; j -= 1
        elif i > 0 and cost[i][j] == cost[i - 1][j] + 1:
            dele += 1; i -= 1
        else:
            ins += 1; j -= 1
    rate = (sub + dele + ins) / n if n else (0.0 if not m else 1.0)
    return {"wer": round(rate, 4), "substitutions": sub, "deletions": dele,
            "insertions": ins, "hits": hits, "ref_words": n, "hyp_words": m}


def transcribe_file(path: str, *, model: str = "large-v3", device: str = "auto",
                    compute_type: str = "default", language: str = "en",
                    initial_prompt: str | None = DEFAULT_INITIAL_PROMPT,
                    word_timestamps: bool = True) -> dict:
    """Gold-transcribe an audio file. Lazy-imports faster-whisper so the wer/grade-math
    paths (and the unit test) work without the model installed."""
    from faster_whisper import WhisperModel

    wm = WhisperModel(model, device=device, compute_type=compute_type)
    segments, info = wm.transcribe(
        path, language=language, initial_prompt=initial_prompt,
        word_timestamps=word_timestamps, vad_filter=True,
    )
    segs = []
    for s in segments:
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                     "text": s.text.strip()})
    return {
        "path": path,
        "model": model,
        "language": info.language,
        "duration_s": round(info.duration, 2),
        "text": " ".join(s["text"] for s in segs).strip(),
        "segments": segs,
    }


def _load_turns(turns_path: str) -> list[dict]:
    """Load a /debug/turns dump (the {"turns":[...]} shape, or a bare list)."""
    with open(turns_path) as f:
        data = json.load(f)
    return data["turns"] if isinstance(data, dict) and "turns" in data else data


def grade(audio: str, turns_path: str, *, model: str = "large-v3") -> dict:
    """Score live caller transcripts against a gold transcription of the recording.

    v1 reports a corpus-level WER: all live caller_texts concatenated (hypothesis) vs
    the gold transcript (reference). NOTE: if the recording is a mixed (not caller-only)
    channel it also contains Aria's speech, which inflates WER — prefer a caller-isolated
    or dual-channel recording. Per-turn alignment is a planned v2.
    """
    gold = transcribe_file(audio, model=model)
    turns = _load_turns(turns_path)
    live = " ".join((t.get("caller_text") or "") for t in turns).strip()
    score = wer(gold["text"], live)
    return {
        "audio": audio,
        "gold_model": model,
        "gold_text": gold["text"],
        "live_text": live,
        "turns": len(turns),
        "corpus_wer": score,
    }


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="stt_grader", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("transcribe", help="gold-transcribe an audio file")
    pt.add_argument("audio")
    pt.add_argument("--model", default="large-v3")
    pt.add_argument("--device", default="auto")
    pt.add_argument("--compute-type", default="default")
    pt.add_argument("--no-hint", action="store_true", help="disable the Settl vocab hint")

    pw = sub.add_parser("wer", help="word error rate between two transcripts")
    pw.add_argument("reference")
    pw.add_argument("hypothesis")

    pg = sub.add_parser("grade", help="score live /debug/turns transcripts vs a recording")
    pg.add_argument("audio")
    pg.add_argument("--turns", required=True, help="path to a saved /debug/turns JSON")
    pg.add_argument("--model", default="large-v3")

    args = p.parse_args(argv)
    if args.cmd == "transcribe":
        out = transcribe_file(args.audio, model=args.model, device=args.device,
                              compute_type=args.compute_type,
                              initial_prompt=None if args.no_hint else DEFAULT_INITIAL_PROMPT)
    elif args.cmd == "wer":
        out = wer(args.reference, args.hypothesis)
    else:
        out = grade(args.audio, args.turns, model=args.model)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
