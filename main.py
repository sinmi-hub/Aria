"""FastAPI app entrypoint.

Loads the audio engines once at startup, then mounts the Telnyx HTTP webhook and
the bidirectional media WebSocket. Run with: ``uvicorn main:app`` (see Makefile).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.runtime import engines
from app.telephony.media_ws import router as media_router
from app.telephony.webhook import router as webhook_router
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    engines.load()
    yield


app = FastAPI(title="voice-agent", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(media_router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "engines_loaded": engines.loaded}


@app.get("/debug/config")
async def debug_config() -> dict:
    """Non-secret runtime config, so the media/streaming URL can be verified remotely
    (image pods have no SSH). If public_url is empty, streaming_start will 422 —
    that's the 'answers but no audio' failure mode."""
    return {
        "public_url": settings.public_url,
        "media_ws_url": settings.media_ws_url,
        "webhook_url": settings.webhook_url,
        "tts_engine": settings.tts_engine,
        "whisper_model": settings.whisper_model,
        "media_ws_token_set": bool(settings.media_ws_token),
        "telnyx_signature_verification": bool(settings.telnyx_public_key),
    }


@app.get("/debug/metrics")
async def debug_metrics() -> dict:
    """Last non-secret turn metrics for quick live-call benchmarking."""
    return engines.last_metrics


@app.get("/debug/gpu")
async def debug_gpu() -> dict:
    """Whole-GPU VRAM + utilization snapshot. Baseline this with whisper+kokoro loaded,
    then again with the Phase 7 SLM loaded, to see headroom and catch contention/degradation
    (which would otherwise only show up as ballooning stt/tts times in the trace)."""
    from app import gpu
    return gpu.snapshot()


@app.get("/debug/logs")
async def debug_logs(n: int = 200) -> dict:
    """Last N in-memory log lines — lets us read a call's logs over the proxy without
    SSH (the golden image has no sshd)."""
    from app.log import recent
    return {"lines": recent(n)}


@app.get("/debug/turns")
async def debug_turns(n: int = 20) -> dict:
    """Structured per-turn traces: caller/Aria transcripts with timestamps, a per-stage
    latency breakdown (vad_tail -> stt -> llm_ttft -> chunk_buffer -> tts -> ttfa),
    per-round LLM input/output, tool calls with timing, and tagged errors."""
    from app import trace
    return {"turns": trace.recent_turns(n)}


@app.get("/debug/errors")
async def debug_errors(n: int = 50) -> dict:
    """Explicitly-tagged error events across recent calls (stage, type, message, turn)."""
    from app import trace
    return {"errors": trace.recent_errors(n)}


_slm_state: dict = {"status": "idle"}
_slm_lock = __import__("threading").Lock()


@app.get("/debug/slm_probe")
async def debug_slm_probe(run: int = 0, model: str = "Qwen/Qwen3-4B-Instruct-2507",
                          dtype: str = "bf16", no_warmup: int = 0, tuned: int = 1) -> dict:
    """Phase 7 SLM probe, driven over HTTP because the image pod has no SSH. With run=1
    it kicks the probe off in a background thread (model load + 6 generations takes a few
    minutes); poll the same endpoint (run=0) to read progress + the final result.
    Params: model=<hf repo id>, dtype=bf16|4bit. Live progress also lands in /debug/logs."""
    import threading

    from app import trace  # noqa: F401  (ensures app package import path is warm)
    from app.log import debug
    from scripts import slm_probe

    if not run:
        return _slm_state

    with _slm_lock:
        if _slm_state.get("status") == "running":
            return {"status": "already_running", **_slm_state}
        _slm_state.clear()
        _slm_state.update({"status": "running", "model": model, "dtype": dtype,
                           "prompt": "tuned" if tuned else "raw", "log": []})

    def _emit(line: str) -> None:
        _slm_state["log"].append(line)
        debug(line)

    def _worker() -> None:
        try:
            result = slm_probe.run(model, dtype, warmup=not no_warmup, emit=_emit, tuned=bool(tuned))
            _slm_state["result"] = result
            _slm_state["status"] = "done"
        except slm_probe.ProbeError as exc:
            _slm_state["error"] = str(exc)
            _slm_state["status"] = "error"
        except Exception as exc:  # noqa: BLE001
            _slm_state["error"] = f"{type(exc).__name__}: {exc}"
            _slm_state["status"] = "error"

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "started", "model": model, "dtype": dtype,
            "poll": "GET /debug/slm_probe (run=0) to read progress + result"}


@app.get("/debug/tools")
async def debug_tools() -> dict:
    """Verify the Google tools actually work on THIS host (image pods have no SSH).
    Builds AriaTools and runs a real lead lookup + slot fetch. Returns names/counts
    only — no secrets. If this is green, a live call's lookup/booking will work."""
    out = {
        "creds_source": "b64" if settings.google_sa_key_b64 else (
            "file" if settings.google_sa_key_path else "none"),
        "leads_sheet_set": bool(settings.leads_sheet_id),
        "calendar_set": bool(settings.demo_calendar_id),
        "timezone": settings.demo_timezone,
    }
    try:
        from app.agent.tools import AriaTools

        t = AriaTools()
        lead = t.store.get_lead_by_phone("4439292703")
        out["tools_enabled"] = True
        out["test_lookup"] = lead.name if lead else "(no match)"
        out["calendar_slots"] = len(t.calendar.find_open_slots(days_ahead=3))
    except Exception as exc:  # noqa: BLE001
        out["tools_enabled"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
