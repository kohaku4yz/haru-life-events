"""Events MCP backend supervisor: the loopback MCP 2.0 Events server plus its
private OpenAI Tunnel client (run by haru-life-events-canary.service; the name is
historical).

It serves events/list and subscription admission for Work. It does not ingest
location or Vitals and never sends webhooks: the context worker does that.
No secrets are printed or copied outside the local private configuration directory.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main() -> int:
    root = pathlib.Path.home() / ".config/haru-life-events"
    config = root / "tunnel.yaml"
    settings = root / "events.env"
    if not config.is_file() or not settings.is_file():
        raise SystemExit("Canary not configured")
    if (root / "control-plane-runtime.key").stat().st_size < 32:
        raise SystemExit("Runtime key missing")
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", 8794))
        except OSError as exc:
            raise SystemExit("Existing listener occupies canary port") from exc

    env = dict(os.environ)
    for line in settings.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    env.update({
        "HARU_EVENTS_BRIDGE_ENABLED": "1",
        "HARU_EVENTS_SAFE_AUDIT_FILE": str(pathlib.Path.home() / ".local/share/haru-life-events/wire-audit.jsonl"),
        "PYTHONPATH": str(pathlib.Path(__file__).resolve().parent.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    python = sys.executable
    stop_requested = False

    def on_stop(signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    os.umask(0o077)
    children: list[subprocess.Popen] = []

    def halt_all():
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)

    try:
        with tempfile.TemporaryDirectory(prefix="haru-events-canary-") as tmp:
            health_file = pathlib.Path(tmp) / "health.url"
            with open(os.devnull, "wb") as devnull:
                events = subprocess.Popen(
                    [python, "-m", "haru_life_events.tunnel_events_local"],
                    env=env, stdout=devnull, stderr=devnull,
                )
                children.append(events)
                for _ in range(70):
                    if stop_requested:
                        return 0
                    if events.poll() is not None:
                        raise RuntimeError("Events backend stopped")
                    try:
                        urllib.request.urlopen(
                            "http://127.0.0.1:8794/mcp", timeout=0.3
                        )
                    except urllib.error.HTTPError as exc:
                        if exc.code in (401, 404):
                            break
                    except (OSError, TimeoutError):
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Events backend startup timeout")

                tunnel = subprocess.Popen(
                    ["/opt/tunnel-client/tunnel-client", "run",
                     "--config", str(config),
                     "--health.listen-addr", "127.0.0.1:0",
                     "--health.url-file", str(health_file),
                     "--log.level", "warn"],
                    stdout=devnull, stderr=devnull,
                )
                children.append(tunnel)

                ready_url = ""
                for _ in range(160):
                    if stop_requested:
                        return 0
                    if tunnel.poll() is not None:
                        raise RuntimeError("Tunnel client stopped before ready")
                    if health_file.is_file():
                        endpoint = health_file.read_text().strip()
                        if endpoint.startswith("http://127.0.0.1:"):
                            ready_url = endpoint.rstrip("/") + "/readyz"
                            try:
                                with urllib.request.urlopen(ready_url, timeout=0.8) as response:
                                    if response.status == 200:
                                        print("HARU_EVENTS_CANARY_READY=200", flush=True)
                                        print("NEW_TUNNEL=connected_and_ready", flush=True)
                                        break
                            except (OSError, urllib.error.HTTPError):
                                pass
                    time.sleep(0.3)
                else:
                    raise RuntimeError("Tunnel startup readiness timeout")

                while not stop_requested:
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("Canary child unexpectedly exited")
                    # No event producers, no delivery worker, no retries.
                    time.sleep(2)
                return 0
    finally:
        halt_all()
        print("HARU_EVENTS_CANARY_STOPPED=true", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # No credentials / private config files ever appear in error output.
        print("HARU_EVENTS_CANARY_ERROR=" + type(exc).__name__, flush=True)
        raise SystemExit(2)
