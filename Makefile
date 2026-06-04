VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install system smoke run setup-telnyx clean

## Install Python deps into a venv. torch+torchaudio are pinned to a matched CPU
## pair from the pytorch CPU index — the default PyPI torchaudio is a CUDA build
## that fails to load on a CPU box and breaks silero-vad.
install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install torch==2.11.0+cpu torchaudio==2.11.0+cpu --index-url https://download.pytorch.org/whl/cpu
	$(PIP) install -r requirements.txt

## System deps: espeak-ng (Kokoro phonemizer fallback) + cloudflared (tunnel).
## cloudflared installs to ~/.local/bin without sudo; espeak-ng needs apt.
system:
	@mkdir -p $(HOME)/.local/bin
	@command -v cloudflared >/dev/null 2>&1 || ( \
		curl -fsSL -o $(HOME)/.local/bin/cloudflared \
		https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
		&& chmod +x $(HOME)/.local/bin/cloudflared )
	sudo apt-get update && sudo apt-get install -y espeak-ng

## Offline pipeline check (codec + VAD + STT + TTS + Claude) — no phone needed.
smoke:
	$(PY) -m scripts.offline_smoke

## Full run: tunnel + Telnyx wiring + server. Then call the printed number.
run:
	bash scripts/start.sh

## (Re)point a Telnyx number at this agent. Needs PUBLIC_URL + TELNYX_API in env.
setup-telnyx:
	$(PY) -m scripts.setup_telnyx

clean:
	rm -rf $(VENV) *.log __pycache__ app/__pycache__ app/*/__pycache__ scripts/__pycache__
