"""GPU telemetry for /debug/gpu — so we can baseline VRAM/util and DETECT degradation
when the SLM (Phase 7 cascade) runs alongside whisper + kokoro on one A4500.

Uses nvidia-smi (present in the CUDA image, reports WHOLE-GPU usage across all processes,
so it catches CTranslate2/faster-whisper which torch's own counter misses). Falls back to
torch.cuda (torch-only allocations) if nvidia-smi isn't available. Never raises.
"""
from __future__ import annotations

import subprocess


def snapshot() -> dict:
    smi = _nvidia_smi()
    if smi:
        return smi
    return _torch_fallback()


def _nvidia_smi() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.used,memory.total,memory.free,utilization.gpu,utilization.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        # One line per GPU; we run a single-GPU pod.
        name, used, total, free, util, memutil = [c.strip() for c in out.stdout.strip().splitlines()[0].split(",")]
        used_mb, total_mb = int(used), int(total)
        return {
            "source": "nvidia-smi",
            "name": name,
            "vram_used_mb": used_mb,
            "vram_total_mb": total_mb,
            "vram_free_mb": int(free),
            "vram_used_pct": round(100 * used_mb / total_mb, 1) if total_mb else None,
            "gpu_util_pct": int(util),
            "mem_util_pct": int(memutil),
        }
    except Exception:  # noqa: BLE001 - telemetry must never break the endpoint
        return None


def _torch_fallback() -> dict:
    try:
        import torch
        if not torch.cuda.is_available():
            return {"source": "none", "error": "cuda not available"}
        total = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
        reserved = torch.cuda.memory_reserved(0) // (1024 * 1024)
        allocated = torch.cuda.memory_allocated(0) // (1024 * 1024)
        return {
            "source": "torch (torch-allocs only; undercounts CTranslate2/whisper)",
            "name": torch.cuda.get_device_name(0),
            "vram_total_mb": total,
            "vram_reserved_mb": reserved,
            "vram_allocated_mb": allocated,
        }
    except Exception as exc:  # noqa: BLE001
        return {"source": "none", "error": str(exc)}
