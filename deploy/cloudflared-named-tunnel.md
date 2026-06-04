# Stable public endpoint for Telnyx (free) — cloudflared named tunnel

Phase 1 used a cloudflared **quick tunnel** (random URL each run). Telnyx needs a
**stable, valid-TLS `wss://`**. A cloudflared **named tunnel** gives that for free,
works behind NAT (important on marketplace GPUs), and needs no open inbound ports.

## Prerequisites
- A Cloudflare account.
- A domain managed in Cloudflare DNS (any domain you control).

## One-time setup
```bash
# On the GPU host (or wherever the app runs):
cloudflared tunnel login                      # opens browser, authorizes the zone
cloudflared tunnel create voice-agent         # creates tunnel + credentials json
cloudflared tunnel route dns voice-agent voice.yourdomain.com
```
Create `~/.cloudflared/config.yml`:
```yaml
tunnel: voice-agent
credentials-file: /root/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: voice.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
```
Run it (or install as a service with `cloudflared service install`):
```bash
cloudflared tunnel run voice-agent
```

## Point the app + Telnyx at the stable URL
```bash
export PUBLIC_URL=https://voice.yourdomain.com
python -m scripts.setup_telnyx     # one-time: sets the Call Control webhook + assigns number
```
`config.py` derives both URLs from `PUBLIC_URL`:
- webhook: `https://voice.yourdomain.com/telnyx/webhook`
- media:   `wss://voice.yourdomain.com/ws/media`

Because the URL no longer changes per run, `scripts/start.sh` should be simplified
for prod: skip the quick-tunnel scraping, assume `PUBLIC_URL` is set, and just start
uvicorn (the named tunnel runs as its own service/container).

## Alternative: host domain + Caddy
If the GPU host has a public IP and you can open :443, run Caddy as a reverse proxy
to `localhost:8000` — it auto-provisions Let's Encrypt TLS. Same `PUBLIC_URL` idea.
