"""One-command RunPod bring-up for the voice agent.

Because the pilot pod has no persistent volume (cheapest option), STOPPING it
resets the container to the base image. This script makes "bring it back up" a
single reliable step: it finds (or creates) the GPU pod, pushes the code, installs
the exact known-good stack, wires Telnyx, launches the self-healing server, and
validates the full call loop with scripts/sim_call.

Usage (from the repo root, with deploy/.env holding RUNPOD_API_KEY):
    python deploy/runpod_up.py                # create-or-reuse + full deploy + validate
    python deploy/runpod_up.py --no-validate  # skip the sim_call check
    python deploy/runpod_up.py --reuse <id>   # force a specific existing pod id

It is idempotent: re-running redeploys onto the same RUNNING pod.
Requires: runpodctl on PATH, ssh/tar locally, ~/.ssh/voiceagent_rp (auto-created).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / "deploy" / ".env"
SSH_KEY = Path.home() / ".ssh" / "voiceagent_rp"
POD_NAME = "voice-agent"
TEMPLATE = "runpod-torch-v220"          # torch2.2/cuda12.1.1/py3.10 base (we upgrade torch)
GPU_CANDIDATES = [
    "NVIDIA RTX A4500", "NVIDIA RTX A5000", "NVIDIA RTX A4000",
    "NVIDIA GeForce RTX 4090", "NVIDIA RTX A6000",
]
DISK_GB = 40
TORCH = "torch==2.5.1+cu121 torchaudio==2.5.1+cu121 --index-url https://download.pytorch.org/whl/cu121"


def env() -> dict:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def ensure_token(e: dict) -> str:
    if e.get("MEDIA_WS_TOKEN"):
        return e["MEDIA_WS_TOKEN"]
    tok = secrets.token_urlsafe(24)
    with ENV_FILE.open("a") as f:
        f.write(f"\nMEDIA_WS_TOKEN={tok}\n")
    print(f"[up] generated MEDIA_WS_TOKEN and saved to deploy/.env")
    return tok


def rp(api_key: str, *args: str) -> str:
    env_ = {**os.environ, "RUNPOD_API_KEY": api_key,
            "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", "")}
    return subprocess.run(["runpodctl", *args], capture_output=True, text=True, env=env_).stdout


def find_pod(api_key: str) -> dict | None:
    out = rp(api_key, "pod", "list", "-o", "json")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    data = data if isinstance(data, list) else [data]
    for p in data:
        if p.get("name") == POD_NAME:
            return p
    return None


def pod_get(api_key: str, pid: str) -> dict:
    return json.loads(rp(api_key, "pod", "get", pid, "-o", "json"))


def create_pod(api_key: str, pubkey: str) -> str:
    env_json = json.dumps({"PUBLIC_KEY": pubkey})
    for gpu in GPU_CANDIDATES:
        print(f"[up] trying GPU: {gpu}")
        out = rp(api_key, "pod", "create", "--name", POD_NAME, "--template-id", TEMPLATE,
                 "--gpu-id", gpu, "--gpu-count", "1", "--cloud-type", "COMMUNITY",
                 "--container-disk-in-gb", str(DISK_GB), "--volume-in-gb", "0",
                 "--ports", "8000/http,22/tcp", "--ssh", "--env", env_json, "-o", "json")
        if '"id"' in out:
            pid = json.loads(out)["id"]
            print(f"[up] created pod {pid} on {gpu}")
            return pid
    sys.exit("[up] no GPU capacity across candidates; try again shortly")


def wait_ssh(api_key: str, pid: str) -> tuple[str, int, str]:
    print("[up] waiting for pod RUNNING + ssh...")
    for _ in range(120):
        d = pod_get(api_key, pid)
        s = d.get("ssh") or {}
        if d.get("desiredStatus") == "RUNNING" and s.get("ip") and s.get("port"):
            proxy = f"https://{pid}-8000.proxy.runpod.net"
            return s["ip"], int(s["port"]), proxy
        time.sleep(3)
    sys.exit("[up] pod never became reachable")


def ssh_base(ip: str, port: int) -> list[str]:
    return ["ssh", "-i", str(SSH_KEY), "-p", str(port),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
            f"root@{ip}"]


def run_ssh(ip: str, port: int, script: str, timeout: int = 900) -> int:
    return subprocess.run(ssh_base(ip, port) + [script], timeout=timeout).returncode


def push_code(ip: str, port: int) -> None:
    print("[up] pushing code...")
    run_ssh(ip, port, "mkdir -p /workspace/voice-agent")
    tar = subprocess.Popen(
        ["tar", "czf", "-", "--exclude=.venv", "--exclude=.git", "--exclude=__pycache__",
         "--exclude=*.log", "--exclude=voices/kokoro-v1.0.onnx",
         "--exclude=voices/kokoro-v1.0.int8.onnx", "--exclude=voices/voices-v1.0.bin", "."],
        cwd=REPO, stdout=subprocess.PIPE)
    subprocess.run(ssh_base(ip, port) + ["tar xzf - -C /workspace/voice-agent"],
                   stdin=tar.stdout, check=True)
    tar.wait()


def push_key(ip: str, port: int, e: dict, retries: int = 6) -> None:
    """Copy the Google service-account key onto the pod at runtime. The key is kept
    OUT of the image (.dockerignore) so it never lands in the public registry; the
    container has no persistent volume, so it must be pushed on every bring-up.
    The image WORKDIR is /app, and GOOGLE_SA_KEY_PATH is relative to it.

    Call this AFTER /health passes — on a big custom image the pod reports RUNNING
    (ssh ip/port assigned) before sshd actually accepts connections. Non-fatal: if it
    can't push, it warns with the manual command rather than killing the deploy."""
    rel = e.get("GOOGLE_SA_KEY_PATH", "")
    local = REPO / rel if rel else None
    if not rel or not local.exists():
        print(f"[up] WARNING: no service-account key to push ({rel or 'unset'}); "
              "agent will run TEXT-ONLY (no lookup/booking).")
        return
    remote = f"/app/{rel}"
    scp = ["scp", "-i", str(SSH_KEY), "-P", str(port),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
           "-o", "ConnectTimeout=10", str(local), f"root@{ip}:{remote}"]
    print("[up] pushing service-account key (runtime secret, not baked)...")
    for attempt in range(1, retries + 1):
        run_ssh(ip, port, f"mkdir -p {os.path.dirname(remote)}")
        if subprocess.run(scp).returncode == 0:
            run_ssh(ip, port, f"chmod 600 {remote}")
            print("[up] key pushed -> {} (chmod 600)".format(remote))
            return
        print(f"[up] key push attempt {attempt}/{retries} failed (sshd warming up); 5s...")
        time.sleep(5)
    print("[up] WARNING: could not push the key. Agent is TEXT-ONLY until you run:\n"
          f"     scp -i {SSH_KEY} -P {port} {local} root@{ip}:{remote}")


def deploy(ip: str, port: int, e: dict, proxy: str, token: str) -> None:
    print("[up] installing deps + building .env (this is the slow step ~3-5min)...")
    script = f"""set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq espeak-ng >/dev/null 2>&1 && echo 'espeak-ng OK'
cd /workspace/voice-agent
pip3 install -q {TORCH} 2>&1 | tail -1
pip3 install -q -r requirements.txt 2>&1 | tail -1
pip3 uninstall -y torchvision >/dev/null 2>&1 || true
cp deploy/.env .env
grep -q '^PIPER_MODEL_PATH=' .env || echo 'PIPER_MODEL_PATH=voices/en_US-amy-medium.onnx' >> .env
grep -q '^AGENT_NUMBER=' .env || echo 'AGENT_NUMBER={e.get("TELNYX_AGENT_NUMBER","+15551234567")}' >> .env
echo 'PUBLIC_URL={proxy}' >> .env
grep -q '^MEDIA_WS_TOKEN=' .env || echo 'MEDIA_WS_TOKEN={token}' >> .env
echo 'DEBUG=1' >> .env
python3 -c 'import torch,kokoro,transformers,nacl,faster_whisper as _; print("deps OK torch",torch.__version__,"cuda",torch.cuda.is_available())'
"""
    if run_ssh(ip, port, script, timeout=900) != 0:
        sys.exit("[up] deploy step failed")


def launch(ip: str, port: int) -> None:
    print("[up] launching self-healing server...")
    run_ssh(ip, port,
            "cd /workspace/voice-agent && pkill -f 'uvicorn main:app' 2>/dev/null; "
            "setsid nohup bash scripts/run_server.sh > server.log 2>&1 < /dev/null & disown; sleep 2; true")


def wait_health(proxy: str, deadline_s: int = 1500) -> bool:
    """Poll {proxy}/health until ok, bounded by WALL-CLOCK time (not iteration count).

    The old iteration cap made the real window unpredictable: failed probes return in
    ~0s during boot (so 360 tries ≈ 12 min) but a slow/cold-host ~7GB image pull can
    take longer, so the loop gave up while the pod was still pulling — a false timeout
    that exit-1'd a deploy that then came up fine a minute later. A 25-min wall-clock
    deadline covers cold pulls; progress lines keep a long wait from looking hung."""
    import urllib.request
    start = time.time()
    next_note = 60.0
    while time.time() - start < deadline_s:
        try:
            with urllib.request.urlopen(f"{proxy}/health", timeout=5) as r:
                if b'"ok":true' in r.read():
                    return True
        except Exception:
            pass
        elapsed = time.time() - start
        if elapsed >= next_note:
            print(f"[up] still waiting for /health... {int(elapsed)}s "
                  "(cold image pull can take 5-10+ min)")
            next_note += 60
        time.sleep(3)
    return False


def wire_telnyx(ip: str, port: int, proxy: str) -> None:
    print("[up] wiring Telnyx...")
    run_ssh(ip, port, f"cd /workspace/voice-agent && PUBLIC_URL={proxy} python3 -m scripts.setup_telnyx")


# --- golden-image mode (no code push / pip install — image has everything) ---------
def _pod_env(pubkey: str, e: dict, token: str, public_url: str = "") -> dict:
    env_ = {"PUBLIC_KEY": pubkey, "ANTHROPIC_API_KEY": e.get("ANTHROPIC_API_KEY", ""),
            "TELNYX_API": e.get("TELNYX_API", ""), "MEDIA_WS_TOKEN": token,
            "DEBUG": e.get("DEBUG", "1"),
            "AGENT_NUMBER": e.get("TELNYX_AGENT_NUMBER", "+15551234567"),
            "TELNYX_PUBLIC_KEY": e.get("TELNYX_PUBLIC_KEY", "")}
    # Phase 4: Google CRM/Calendar + operator config (non-secret). Carried from deploy/.env.
    for k in ("LEADS_SHEET_ID", "LEADS_SHEET_TAB", "DEMO_CALENDAR_ID",
              "DEMO_TIMEZONE", "OPERATOR_NAME", "OPERATOR_EMAIL"):
        if e.get(k):
            env_[k] = e[k]
    # Phase 6: OpenAI Realtime backend select + key/model/voice. Only matters when
    # REALTIME_BACKEND=openai; harmless otherwise.
    for k in ("REALTIME_BACKEND", "OPENAI_API_KEY", "OPENAI_REALTIME_MODEL", "OPENAI_VOICE"):
        if e.get(k):
            env_[k] = e[k]
    # Phase 7: HF token for the SLM probe (only needed if a candidate model is gated;
    # Qwen3 is public). Carried from deploy/.env if present.
    if e.get("HF_TOKEN"):
        env_["HF_TOKEN"] = e["HF_TOKEN"]
    # Phase 7: local SLM cascade flags (non-secret). Only matter when BRAIN_BACKEND=slm +
    # REALTIME_BACKEND=local; harmless otherwise. SLM_* fall back to config.py defaults.
    for k in ("BRAIN_BACKEND", "STT_STREAMING", "SLM_KV_PREWARM",
              "SLM_MODEL", "SLM_DTYPE", "SLM_MAX_NEW_TOKENS"):
        if e.get(k):
            env_[k] = e[k]
    # The SA key goes in as base64 (the golden image has no sshd to scp a file into).
    rel = e.get("GOOGLE_SA_KEY_PATH", "")
    if rel and (REPO / rel).exists():
        import base64
        env_["GOOGLE_SA_KEY_B64"] = base64.b64encode((REPO / rel).read_bytes()).decode()
    else:
        print(f"[up] WARNING: SA key {rel or '<unset>'} not found — agent will be TEXT-ONLY")
    if public_url:
        env_["PUBLIC_URL"] = public_url
    return env_


def create_pod_image(api_key: str, image: str, pubkey: str, e: dict, token: str) -> str:
    # No PUBLIC_URL: the image's entrypoint creates its own cloudflared tunnel (the
    # RunPod proxy is unreliable for Telnyx WebSockets) and self-wires Telnyx.
    pod_env = _pod_env(pubkey, e, token)
    for gpu in GPU_CANDIDATES:
        print(f"[up] (image) trying GPU: {gpu}")
        out = rp(api_key, "pod", "create", "--name", POD_NAME, "--image", image,
                 "--gpu-id", gpu, "--gpu-count", "1", "--cloud-type", "COMMUNITY",
                 "--container-disk-in-gb", str(DISK_GB), "--volume-in-gb", "0",
                 "--ports", "8000/http,22/tcp", "--ssh", "--env", json.dumps(pod_env), "-o", "json")
        if '"id"' in out:
            pid = json.loads(out)["id"]
            print(f"[up] created image pod {pid} on {gpu}")
            return pid
    sys.exit("[up] no GPU capacity across candidates; try again shortly")


def wait_running(api_key: str, pid: str) -> str:
    print("[up] waiting for pod RUNNING...")
    for _ in range(120):
        if pod_get(api_key, pid).get("desiredStatus") == "RUNNING":
            return f"https://{pid}-8000.proxy.runpod.net"
        time.sleep(3)
    sys.exit("[up] pod never reached RUNNING")


def _read_config(proxy: str) -> dict:
    """Read /debug/config over the RunPod proxy (HTTP works fine; only WS is flaky)."""
    import urllib.request
    try:
        return json.load(urllib.request.urlopen(f"{proxy}/debug/config", timeout=10))
    except Exception as exc:  # noqa: BLE001 (older images lack /debug/config)
        print(f"[up] (could not read /debug/config: {exc})")
        return {}


def run_image_mode(api_key: str, image: str, pubkey: str, e: dict, token: str,
                   do_validate: bool, forced: str | None) -> None:
    pod = {"id": forced} if forced else find_pod(api_key)
    if pod and pod.get("desiredStatus") != "RUNNING":
        print(f"[up] starting stopped pod {pod['id']}...")
        rp(api_key, "pod", "start", pod["id"])
    pid = pod["id"] if pod else create_pod_image(api_key, image, pubkey, e, token)
    ip, port, proxy = wait_ssh(api_key, pid)  # proxy = HTTP-only (health/debug); ssh for key push
    print(f"[up] pod {pid}  ssh root@{ip}:{port}  http {proxy}  (image: {image})")
    print("[up] entrypoint runs its own cloudflared tunnel + self-wires Telnyx; waiting "
          "for health (first boot pulls the ~7GB image, can take 5-10 min)...")
    healthy = wait_health(proxy)
    # Non-fatal: the pod is already provisioned. A health timeout usually just means a
    # slow cold-host pull that finishes shortly — don't exit-1 a deploy that succeeded.
    if not healthy:
        print(f"[up] WARNING: /health not confirmed within the window. The pod is up and "
              f"usually finishes booting shortly — verify: {proxy}/health and {proxy}/debug/tools")
    # Key arrives via GOOGLE_SA_KEY_B64 in the pod env (image has no sshd for scp).
    # The real public endpoint is the cloudflared URL the container made — read it back.
    cfg = _read_config(proxy) if healthy else {}
    public, mws = cfg.get("public_url", ""), cfg.get("media_ws_url", "")
    if healthy and not public:
        print("[up] WARNING: no public_url — cloudflared tunnel may have failed; check pod logs")
    elif public:
        print(f"[up] HEALTHY. public_url={public}\n[up] media_ws_url={mws}")
    if healthy and do_validate and mws:
        print("[up] validating full call loop with sim_call against the tunnel URL...")
        subprocess.run([sys.executable, "-m", "scripts.sim_call", mws,
                        "--say", "Hi, what can you help me with?"], cwd=REPO)
    print("\n=== voice agent is UP (golden image + cloudflared) ===")
    print(f"  pod:        {pid}  (${pod_get(api_key, pid).get('costPerHr')}/hr)")
    print(f"  public_url: {public or '<tunnel failed — check logs>'}")
    print(f"  call:       {e.get('TELNYX_AGENT_NUMBER', '+15551234567')}")
    print(f"  stop:       runpodctl pod stop {pid}")


def main() -> None:
    args = sys.argv[1:]
    do_validate = "--no-validate" not in args
    forced = args[args.index("--reuse") + 1] if "--reuse" in args else None

    image = args[args.index("--image") + 1] if "--image" in args else None

    e = env()
    api_key = e.get("RUNPOD_API_KEY") or sys.exit("[up] RUNPOD_API_KEY missing in deploy/.env")
    token = ensure_token(e)
    if not SSH_KEY.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(SSH_KEY),
                        "-C", "voice-agent-runpod"], check=True)
    pubkey = (SSH_KEY.with_suffix(".pub")).read_text().strip()

    if image:
        run_image_mode(api_key, image, pubkey, e, token, do_validate, forced)
        return

    pod = {"id": forced} if forced else find_pod(api_key)
    if pod and pod.get("desiredStatus") != "RUNNING":
        print(f"[up] starting stopped pod {pod['id']}...")
        rp(api_key, "pod", "start", pod["id"])
    pid = pod["id"] if pod else create_pod(api_key, pubkey)

    ip, port, proxy = wait_ssh(api_key, pid)
    print(f"[up] pod {pid}  ssh root@{ip}:{port}  url {proxy}")
    push_code(ip, port)
    deploy(ip, port, e, proxy, token)
    launch(ip, port)
    healthy = wait_health(proxy)  # non-fatal — see run_image_mode note
    if healthy:
        print(f"[up] HEALTHY: {proxy}/health")
    else:
        print(f"[up] WARNING: /health not confirmed in window; the pod is up — "
              f"verify {proxy}/health shortly.")
    wire_telnyx(ip, port, proxy)
    if healthy and do_validate:
        validate(proxy, token)
    print("\n=== voice agent is UP ===")
    print(f"  pod:    {pid}  (${pod_get(api_key, pid).get('costPerHr')}/hr)")
    print(f"  url:    {proxy}")
    print(f"  call:   {e.get('TELNYX_AGENT_NUMBER', '+15551234567')}")
    print(f"  stop:   runpodctl pod stop {pid}   (resets container; re-run this script to bring back)")


if __name__ == "__main__":
    main()
