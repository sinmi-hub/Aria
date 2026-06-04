"""Ground-truth call-timing oracle for Aria.

Telnyx records every call dual-channel (caller on one track, Aria on the other —
``record_start ... channels=dual`` in ``app/telephony/commands.py``). Because the
two speakers are already isolated, we can measure the real perceived latency —
caller-stops-talking -> Aria-first-audio — straight from the audio the caller
actually heard, independent of the app's own ``/debug/turns`` trace.

Pipeline: pull the dual-channel mp3 from the Telnyx API -> split into two mono
16 kHz tracks with ffmpeg -> energy-VAD each track for precise speech on/offsets
(the authoritative timing) -> faster-whisper word timestamps for human-readable
turn labels -> walk the merged timeline and report each caller->Aria gap.

Usage::

    python scripts/call_timing.py list [--limit N]
    python scripts/call_timing.py pull <recording_id|latest> [-o out.mp3]
    python scripts/call_timing.py time <recording_id|latest|path.mp3> [--swap] [--json]

All external processes run via argv arrays (no shell), so nothing here is
injectable the way the audited whisper-mcp fork was.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# config import pulls the Telnyx key + whisper settings the live pipeline uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402

TELNYX_BASE = "https://api.telnyx.com/v2"
SR = 16000                       # whisper wants 16 kHz mono
# Markers that only Aria says in the opener — used to auto-label the two tracks.
ARIA_MARKERS = ("settl", "aria", "recorded", "moving")


# --------------------------------------------------------------------------- #
# Telnyx recordings API
# --------------------------------------------------------------------------- #
def _telnyx_get(path: str, params: dict | None = None) -> dict:
    import httpx

    headers = {"Authorization": f"Bearer {settings.telnyx_api_key}"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{TELNYX_BASE}{path}", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


def telnyx_list(limit: int = 10) -> list[dict]:
    data = _telnyx_get("/recordings", {"page[size]": limit}).get("data", [])
    # API returns newest-ish first; sort explicitly by start time, newest first.
    return sorted(data, key=lambda r: r.get("recording_started_at") or "", reverse=True)


def _resolve_mp3_url(spec: str) -> tuple[str, str]:
    """Return (recording_id, mp3_url) for 'latest' or a recording id."""
    recs = telnyx_list(20)
    if not recs:
        raise SystemExit("No Telnyx recordings found.")
    rec = recs[0] if spec == "latest" else next((r for r in recs if r.get("id") == spec), None)
    if rec is None:
        raise SystemExit(f"Recording {spec!r} not found in the 20 most recent.")
    url = (rec.get("download_urls") or {}).get("mp3")
    if not url:
        raise SystemExit(f"Recording {rec.get('id')} has no mp3 download url.")
    return rec.get("id"), url


def telnyx_download(spec: str, dest: Path) -> Path:
    import httpx

    rec_id, url = _resolve_mp3_url(spec)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    print(f"Downloaded {rec_id} -> {dest} ({dest.stat().st_size} bytes)")
    return dest


# --------------------------------------------------------------------------- #
# Audio decode (ffmpeg, argv array — no shell)
# --------------------------------------------------------------------------- #
def _probe_channels(mp3: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(mp3)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 1


def _decode_channel(mp3: Path, channel: int) -> np.ndarray:
    """Decode one channel of the mp3 to float32 mono at 16 kHz."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mp3),
         "-af", f"pan=mono|c0=c{channel}", "-ar", str(SR),
         "-f", "f32le", "-acodec", "pcm_f32le", "-"],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on channel {channel}: {proc.stderr.decode()[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


# --------------------------------------------------------------------------- #
# Energy VAD — authoritative speech on/offsets on an isolated channel
# --------------------------------------------------------------------------- #
@dataclass
class Segment:
    start: float            # seconds
    end: float
    text: str = ""


# --- Silero VAD segmentation (the Daily.co benchmark standard, ~30ms precision) ----------
_SILERO = None


def _silero_model():
    global _SILERO
    if _SILERO is None:
        from silero_vad import load_silero_vad
        _SILERO = load_silero_vad()
    return _SILERO


def silero_segments(samples: np.ndarray) -> list["Segment"]:
    """Speech segments via Silero VAD — a learned speech/non-speech classifier, so it does
    NOT clip trailing quiet phonemes the way an energy threshold does. This is the standard
    method for voice-to-voice latency benchmarks (record per-channel, Silero-mark, pair).
    speech_pad_ms=0 so the measured segment end is the real end of speech, not padded."""
    from silero_vad import get_speech_timestamps

    ts = get_speech_timestamps(
        samples, _silero_model(), sampling_rate=SR, return_seconds=True,
        min_silence_duration_ms=200, speech_pad_ms=0)
    return [Segment(t["start"], t["end"]) for t in ts]


def energy_vad(samples: np.ndarray, frame_ms: int = 20,
               min_speech_ms: int = 120, min_gap_ms: int = 200) -> list[Segment]:
    """Return speech segments via frame RMS thresholding.

    On an isolated channel the only signal is one speaker, so a relative
    threshold over the noise floor cleanly recovers on/offsets. ``min_gap_ms``
    bridges intra-utterance pauses; ``min_speech_ms`` drops clicks/breaths.
    """
    if samples.size == 0:
        return []
    hop = int(SR * frame_ms / 1000)
    n_frames = samples.size // hop
    if n_frames == 0:
        return []
    frames = samples[: n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)

    noise = np.percentile(rms, 20)          # quiet-frame floor
    peak = np.percentile(rms, 99)           # loud-speech reference
    threshold = max(noise * 4.0, peak * 0.06, 1e-4)
    voiced = rms > threshold

    segments: list[Segment] = []
    bridge = int(min_gap_ms / frame_ms)
    run_start: int | None = None
    silence = 0
    for i, is_voice in enumerate(voiced):
        if is_voice:
            if run_start is None:
                run_start = i
            silence = 0
        elif run_start is not None:
            silence += 1
            if silence > bridge:
                segments.append(Segment(run_start * frame_ms / 1000,
                                        (i - silence + 1) * frame_ms / 1000))
                run_start = None
                silence = 0
    if run_start is not None:
        segments.append(Segment(run_start * frame_ms / 1000, n_frames * frame_ms / 1000))

    min_speech_s = min_speech_ms / 1000
    return [s for s in segments if (s.end - s.start) >= min_speech_s]


# --------------------------------------------------------------------------- #
# Whisper word timestamps — for human-readable turn labels
# --------------------------------------------------------------------------- #
_MODEL = None


def _whisper():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                              compute_type=settings.whisper_compute_type)
    return _MODEL


def transcribe_text(samples: np.ndarray) -> str:
    if samples.size == 0:
        return ""
    segments, _ = _whisper().transcribe(samples, language="en", beam_size=1,
                                        vad_filter=False, condition_on_previous_text=False)
    return " ".join(seg.text.strip() for seg in segments).strip()


def label_words(samples: np.ndarray, vad_segments: list[Segment]) -> None:
    """Attach the spoken text to each VAD segment (in-place) from word timestamps."""
    if samples.size == 0 or not vad_segments:
        return
    segments, _ = _whisper().transcribe(samples, language="en", beam_size=1,
                                        vad_filter=False, word_timestamps=True,
                                        condition_on_previous_text=False)
    words = [(w.start, w.end, w.word) for seg in segments for w in (seg.words or [])]
    for vs in vad_segments:
        hits = [w for s, e, w in words if vs.start - 0.3 <= s <= vs.end + 0.3]
        vs.text = "".join(hits).strip()


# --------------------------------------------------------------------------- #
# Turn-taking analysis
# --------------------------------------------------------------------------- #
# Micro-acks ("Mm-hm.", "One sec.") are first SOUND but not the answer. We split the two
# because perceived responsiveness tracks time-to-first-sound while time-to-answer is the
# real latency — the operator heard the ack and felt it snappy even when content lagged.
def _ack_set() -> set[str]:
    extras = ("mm", "mmhm", "mmhmm", "mhm", "uh huh", "yep", "yeah", "got it", "one second",
              "just a sec", "hold on", "sure thing")
    phrases = list(settings.micro_ack_phrases) + list(extras)
    return {_norm_text(p) for p in phrases if _norm_text(p)}


def _norm_text(s: str) -> str:
    return " ".join(c for c in "".join(ch if ch.isalnum() else " " for ch in s.lower()).split())


_ACKS = None


def _is_ack(seg: "Segment") -> bool:
    """True if a segment is a micro-ack/filler, not substantive content."""
    global _ACKS
    if _ACKS is None:
        _ACKS = _ack_set()
    norm = _norm_text(seg.text)
    if not norm:
        return True  # unintelligible blip — a sound, not content
    if norm in _ACKS:
        return True
    return (seg.end - seg.start) < 0.6 and len(norm.split()) <= 2


@dataclass
class Turn:
    caller_text: str
    aria_text: str          # the substantive (content) reply text
    caller_end: float       # seconds — caller's last audio on their track
    first_sound: float      # seconds — Aria's FIRST audio after it (may be a micro-ack)
    content_start: float    # seconds — Aria's first SUBSTANTIVE audio
    acked: bool
    first_sound_ms: float = field(init=False)
    content_ms: float = field(init=False)

    def __post_init__(self) -> None:
        self.first_sound_ms = round((self.first_sound - self.caller_end) * 1000, 1)
        self.content_ms = round((self.content_start - self.caller_end) * 1000, 1)


def compute_turns(caller: list[Segment], aria: list[Segment]) -> list[Turn]:
    """Per caller utterance: gap to Aria's first SOUND and to her first SUBSTANTIVE reply."""
    turns: list[Turn] = []
    for cseg in caller:
        nxt = next((c for c in caller if c.start > cseg.end + 0.05), None)
        bound = nxt.start if nxt is not None else float("inf")
        replies = [a for a in aria if cseg.end - 0.05 <= a.start < bound]
        if not replies:
            continue
        # Skip if the caller starts again before Aria says anything (reply is a later turn's).
        if nxt is not None and nxt.start < replies[0].start:
            continue
        first = replies[0]
        content = next((a for a in replies if not _is_ack(a)), first)
        turns.append(Turn(cseg.text, content.text, cseg.end, first.start, content.start,
                          acked=content is not first))
    return turns


def analyze(mp3: Path, swap: bool = False, vad: str = "silero") -> dict:
    n_ch = _probe_channels(mp3)
    if n_ch < 2:
        raise SystemExit(f"{mp3} is mono ({n_ch}ch) — need dual-channel for clean timing.")

    ch0, ch1 = _decode_channel(mp3, 0), _decode_channel(mp3, 1)
    # Auto-label: the track whose opener mentions Settl/Aria/recorded is Aria.
    t0, t1 = transcribe_text(ch0[: SR * 12]), transcribe_text(ch1[: SR * 12])
    score0 = sum(m in t0.lower() for m in ARIA_MARKERS)
    score1 = sum(m in t1.lower() for m in ARIA_MARKERS)
    aria_is_ch0 = score0 >= score1
    if swap:
        aria_is_ch0 = not aria_is_ch0
    aria_samples, caller_samples = (ch0, ch1) if aria_is_ch0 else (ch1, ch0)

    segment = silero_segments if vad == "silero" else energy_vad
    aria_segs, caller_segs = segment(aria_samples), segment(caller_samples)
    label_words(aria_samples, aria_segs)
    label_words(caller_samples, caller_segs)
    turns = compute_turns(caller_segs, aria_segs)

    def _stats(vals: list[float]) -> dict:
        vals = [v for v in vals if v >= 0]
        if not vals:
            return {}
        arr = np.array(vals)
        return {"count": len(vals), "median_ms": round(float(np.median(arr)), 1),
                "p95_ms": round(float(np.percentile(arr, 95)), 1),
                "min_ms": round(float(arr.min()), 1), "max_ms": round(float(arr.max()), 1)}

    return {
        "file": str(mp3),
        "vad": vad,
        "aria_channel": 0 if aria_is_ch0 else 1,
        "label_confidence": {"ch0_aria_markers": score0, "ch1_aria_markers": score1},
        "turns": [{"caller": t.caller_text, "aria": t.aria_text,
                   "caller_end_s": round(t.caller_end, 2), "acked": t.acked,
                   "first_sound_ms": t.first_sound_ms, "content_ms": t.content_ms}
                  for t in turns],
        "stats_first_sound": _stats([t.first_sound_ms for t in turns]),
        "stats_content": _stats([t.content_ms for t in turns]),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_report(result: dict) -> None:
    print(f"\nFile: {result['file']}  (vad={result.get('vad', 'energy')})")
    print(f"Aria = channel {result['aria_channel']}  "
          f"(markers ch0={result['label_confidence']['ch0_aria_markers']} "
          f"ch1={result['label_confidence']['ch1_aria_markers']})")
    print(f"\n{'sound':>7} {'content':>8}  caller -> aria")
    print("  " + "-" * 64)
    for t in result["turns"]:
        caller = (t["caller"][:30] or "·").ljust(30)
        aria = (t["aria"][:20] or "·")
        ack = " (ack)" if t["acked"] else ""
        print(f"{t['first_sound_ms']:>7.0f} {t['content_ms']:>8.0f}  {caller} -> {aria}{ack}")
    print("  " + "-" * 64)
    fs, ct = result.get("stats_first_sound", {}), result.get("stats_content", {})
    if ct:
        print(f"  time-to-first-sound: median={fs.get('median_ms')}ms  p95={fs.get('p95_ms')}ms")
        print(f"  time-to-content:     median={ct.get('median_ms')}ms  p95={ct.get('p95_ms')}ms"
              f"  (n={ct.get('count')})")
    else:
        print("  (no caller->aria turns detected)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ground-truth Aria call-timing oracle.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent Telnyx recordings")
    p_list.add_argument("--limit", type=int, default=10)

    p_pull = sub.add_parser("pull", help="download a recording's mp3")
    p_pull.add_argument("spec", help="recording id or 'latest'")
    p_pull.add_argument("-o", "--out", default=None)

    p_time = sub.add_parser("time", help="measure caller->aria gaps")
    p_time.add_argument("spec", help="recording id, 'latest', or a path to an mp3")
    p_time.add_argument("--swap", action="store_true", help="flip caller/Aria channel labels")
    p_time.add_argument("--vad", choices=["silero", "energy"], default="silero",
                        help="segment boundary detector (silero = benchmark standard)")
    p_time.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    args = parser.parse_args()

    if args.cmd == "list":
        for r in telnyx_list(args.limit):
            dur = (r.get("duration_millis") or 0) / 1000
            print(f"{r.get('id')}  {r.get('recording_started_at')}  "
                  f"{r.get('channels')}  {dur:.1f}s")
        return

    if args.cmd == "pull":
        out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / f"aria_{args.spec}.mp3"
        telnyx_download(args.spec, out)
        return

    if args.cmd == "time":
        spec = args.spec
        if spec in ("latest",) or not Path(spec).exists():
            mp3 = telnyx_download(spec, Path(tempfile.gettempdir()) / f"aria_{spec}.mp3")
        else:
            mp3 = Path(spec)
        result = analyze(mp3, swap=args.swap, vad=args.vad)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_report(result)


if __name__ == "__main__":
    main()
