# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python FastAPI voice agent for Telnyx calls. `main.py` creates the app, loads audio engines on startup, and mounts the HTTP webhook and media WebSocket routers. Configuration and persona text live in `config.py`.

Core code is under `app/`: `app/audio/` contains codec, VAD, STT, and TTS adapters; `app/agent/` contains Claude streaming, sentence chunking, and orchestration; `app/telephony/` contains Telnyx protocol, commands, webhook, signature, and WebSocket handling. Operational scripts are in `scripts/`, deployment assets and runbooks are in `deploy/`, and local voice model files are in `voices/`.

## Build, Test, and Development Commands

- `make system`: installs system dependencies such as `espeak-ng` and `cloudflared`.
- `make install`: creates `.venv`, installs pinned CPU Torch/Torchaudio, then installs `requirements.txt`.
- `make smoke`: runs `python -m scripts.offline_smoke` for the local pipeline check.
- `make run`: starts tunnel setup, Telnyx wiring, and the FastAPI server through `scripts/start.sh`.
- `make setup-telnyx`: repoints the configured Telnyx number using `PUBLIC_URL` and `TELNYX_API`.
- `make clean`: removes local virtualenv, logs, and Python cache directories.

For direct server debugging, use `.venv/bin/uvicorn main:app --reload`.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints for public functions, and small modules with clear boundaries. Prefer `snake_case` for functions, variables, and files; use `PascalCase` for dataclasses or classes. Keep tunable values in `config.py`. Existing modules use concise docstrings and comments only where behavior is non-obvious.

## Testing Guidelines

There is no formal unit test suite yet. Treat `make smoke` as the required regression check for changes that touch audio, agent, config, or telephony behavior. Add focused tests under a future `tests/` directory using `test_*.py` naming when logic can run without live Telnyx or model downloads. Avoid tests that require real API keys unless explicitly marked as integration checks.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries, often with a scope prefix such as `Dockerfile:`, `Fix:`, or `Doc:`. Follow that style and describe the changed behavior.

Pull requests should include purpose, commands run (`make smoke`, manual call test, or deployment check), relevant configuration notes, and logs when changing live-call, tunnel, or deployment behavior.

## Security & Configuration Tips

Do not commit `.env`, API keys, Telnyx tokens, tunnel URLs, generated logs, or call audio artifacts. Keep secrets in environment variables such as `ANTHROPIC_API_KEY`, `TELNYX_API`, `TELNYX_PUBLIC_KEY`, and `MEDIA_WS_TOKEN`. When changing public endpoints, verify `/debug/config` exposes only non-secret values.
