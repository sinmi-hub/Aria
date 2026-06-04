"""Download the STT + TTS model weights at image-build time so the container is
fully self-contained (a "golden image"): no first-call download, reproducible.

Runs CPU-only (build machines have no GPU) — it only populates the HF/CT2 caches;
at runtime the same cached weights load onto the GPU. Silero VAD ships inside its
wheel, so it needs no prefetch.
"""
from __future__ import annotations


def main() -> None:
    # Bake whatever STT model the runtime is configured to use (e.g. medium.en) so
    # the image matches the runtime profile with no first-call download. Reads env
    # directly (no config import) so it works as a standalone build-time script.
    import os

    model = os.environ.get("WHISPER_MODEL", "small.en")
    voice = os.environ.get("KOKORO_VOICE", "af_heart")
    print(f"[prefetch] faster-whisper {model} ...")
    from faster_whisper import WhisperModel

    WhisperModel(model, device="cpu", compute_type="int8")

    print("[prefetch] Kokoro (PyTorch) weights + default voice ...")
    try:
        from kokoro import KPipeline

        pipeline = KPipeline(lang_code="a")  # American English (config.kokoro_lang)
        list(pipeline("Ready.", voice=voice, speed=1))
    except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
        print(f"[prefetch] kokoro skipped ({exc!r}); will download at first run")

    print("[prefetch] done")


if __name__ == "__main__":
    main()
