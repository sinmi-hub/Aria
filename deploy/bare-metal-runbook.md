# Bare-metal deploy runbook (no Docker)

Deploy the voice agent directly on a GPU VM you SSH into. This is the recommended
path for a single, operator-managed box. ~20 minutes, done once.

## 0. Pick a GPU VM (with SSH, not a container pod)
Any of: **Lambda Labs**, **Paperspace**, **AWS g4dn.xlarge/g5** (you're already on
AWS), or a **Hetzner GPU dedicated** (flat monthly). A **T4 / RTX 3060-class** GPU is
plenty for Kokoro-82M + faster-whisper. Use Ubuntu 22.04. Most GPU images ship with
the NVIDIA driver preinstalled.

## 1. Verify the GPU
```bash
nvidia-smi          # must list your GPU + a CUDA version
```
If no driver: install the provider's NVIDIA driver package first (provider-specific).

## 2. Get the code + a CUDA Python env
```bash
git clone <your repo> voice-agent   # or scp the ~/voice-agent folder up
cd voice-agent
python3 -m venv .venv && . .venv/bin/activate
pip install --upgrade pip
# CUDA torch (NOT the Phase-1 CPU pin). cu121 matches CUDA 12.x drivers:
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt
sudo apt-get update && sudo apt-get install -y espeak-ng   # Kokoro phonemizer
```

## 3. GPU profile in .env
```bash
cp deploy/.env .env        # secrets already filled in deploy/.env
# Ensure these are set (deploy/.env already has them):
#   WHISPER_DEVICE=cuda  WHISPER_COMPUTE=float16  WHISPER_MODEL=small.en
#   TTS_ENGINE=kokoro
```
Models: faster-whisper downloads on first load; Kokoro PyTorch downloads from HF on
first load. (kokoro-onnx files are in `voices/` if you choose Option B in SPEC §4.)

## 4. Prove it loads on GPU
```bash
make smoke          # engines should load on cuda; TTS first sentence < ~200ms
```

## 5. Stable public endpoint
Use a **cloudflared named tunnel** (free, no open ports) — see
`cloudflared-named-tunnel.md` — or **Caddy** if the VM has a public IP and you can
open :443. Either way you get a fixed `PUBLIC_URL` like
`https://voice.yourdomain.com`.

## 6. Run under systemd (survives reboots, auto-restart)
`/etc/systemd/system/voice-agent.service`:
```ini
[Unit]
Description=Voice Agent
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/voice-agent
EnvironmentFile=/home/ubuntu/voice-agent/.env
Environment=PUBLIC_URL=https://voice.yourdomain.com
ExecStart=/home/ubuntu/voice-agent/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now voice-agent
journalctl -u voice-agent -f        # logs
```
(Install cloudflared as its own service too: `cloudflared service install`.)

## 7. Wire Telnyx once
```bash
cd /home/ubuntu/voice-agent
PUBLIC_URL=https://voice.yourdomain.com .venv/bin/python -m scripts.setup_telnyx
```
Because the URL is now stable, this is a one-time step (not per-run).

## 8. Validate (Phase-2 done)
Call **+15551234567** → natural Kokoro voice, first audio < ~1.5s after you stop
speaking, barge-in works. Record the GPU $/hr and projected monthly.

## Cost control
- Marketplace/hourly VM: stop it when idle, or schedule business-hours only.
- Hetzner-style flat monthly: simplest to reason about; no hourly surprises.
- AWS: use spot + an auto-stop schedule.
