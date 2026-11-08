"""Real tunnel-client dev proxy -> localhost MCP2 app, entirely disposable.

Set HARU_TUNNEL_DEV_PROXY_BIN to the local tunnel-client executable to opt in.
CI without the binary env var skips this test safely. No real tunnel ID/API key
or callback endpoint is needed, and no service is installed or left running.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_local_target(url: str, process: subprocess.Popen, timeout: float = 8) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError("isolated target exited")
        try:
            urllib.request.urlopen(url, timeout=0.3)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 404):
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.05)
    raise RuntimeError("isolated target failed to start")


def _stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)


class RealDevTunnelTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv("HARU_TUNNEL_DEV_PROXY_BIN"), "opt-in binary unavailable")
    def test_mcp2_protocol_and_authentication_forwarding(self):
        bin_path = os.environ["HARU_TUNNEL_DEV_PROXY_BIN"]
        if not Path(bin_path).is_file():
            self.skipTest("configured dev proxy executable unavailable")
        with tempfile.TemporaryDirectory(prefix="haru-events-tunnel-test-") as td:
            root = Path(td)
            for name, value in (
                ("api", b"T" * 40),
                ("vault", Fernet.generate_key()),
                ("identity", b"I" * 40),
            ):
                file = root / name
                file.write_bytes(value)
                file.chmod(0o600)
            port = _free_port()
            env = {
                **os.environ,
                "HARU_EVENTS_BRIDGE_ENABLED": "1",
                "HARU_EVENTS_OWNER": "owner-canary",
                "HARU_EVENTS_API_KEY_FILE": str(root / "api"),
                "HARU_EVENTS_FERNET_KEY_FILE": str(root / "vault"),
                "HARU_EVENTS_ID_KEY_FILE": str(root / "identity"),
                "HARU_EVENTS_SUBSCRIPTIONS_DB": str(root / "subs.sqlite"),
                "HARU_EVENTS_LOOPBACK_PORT": str(port),
            }
            target = None
            proxy = None
            try:
                target = subprocess.Popen(
                    [sys.executable, "-m", "haru_life_events.tunnel_events_local"],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                _wait_for_local_target(f"http://127.0.0.1:{port}/mcp", target)
                output_file = root / "proxy.json"
                proxy = subprocess.Popen(
                    [bin_path, "dev", "proxy",
                     "--mcp-server-url", f"http://127.0.0.1:{port}/mcp",
                     "--listen", "127.0.0.1:0",
                     "--url-file", str(output_file),
                     "--duration", "22s",
                     "--readiness-timeout", "6s",
                     "--response-timeout", "7s"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                info = None
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    if output_file.exists():
                        try:
                            info = json.loads(output_file.read_text())
                        except (ValueError, OSError):
                            pass
                        if info:
                            break
                    if proxy.poll() is not None:
                        break
                    time.sleep(0.06)
                self.assertIsInstance(info, dict, "dev proxy failed to become ready")
                endpoint = info["mcp_url"]
                parts = urlsplit(endpoint)
                self.assertEqual(parts.hostname, "127.0.0.1")
                self.assertEqual(parts.scheme, "http")
                self.assertTrue(parts.path.startswith("/v1/mcp/"))

                def send(method: str, *, bearer: bool = True, route: str | None = None,
                         params: dict | None = None):
                    body = {
                        "jsonrpc": "2.0", "id": 17, "method": method,
                        "params": {
                            **(params or {}),
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                "io.modelcontextprotocol/clientCapabilities": {},
                            },
                        }
                    }
                    headers = {
                        "MCP-Protocol-Version": "2026-07-28",
                        "Mcp-Method": route or method,
                        "Content-Type": "application/json",
                    }
                    if bearer:
                        headers["Authorization"] = "Bearer " + "T" * 40
                    req = urllib.request.Request(
                        endpoint, json.dumps(body).encode("utf-8"),
                        headers=headers, method="POST"
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            return resp.status, json.load(resp)
                    except urllib.error.HTTPError as e:
                        return e.code, json.loads(e.read().decode("utf-8"))

                status, response = send("server/discover")
                self.assertEqual(status, 200)
                self.assertEqual(response["result"]["supportedVersions"], ["2026-07-28"])
                self.assertIn("events", response["result"]["capabilities"])

                status, response = send("events/list")
                self.assertEqual(status, 200)
                names = {e["name"] for e in response["result"]["events"]}
                self.assertIn("life.location.changed", names)
                self.assertIn("life.context.changed", names)

                status, _ = send("events/list", bearer=False)
                self.assertEqual(status, 401, "tunnel unexpectedly removed authentication gate")
                status, _ = send("events/list", route="events/subscribe")
                self.assertEqual(status, 400, "method routing header mismatch must fail")
                status, response = send("events/subscribe", params={
                    "name": "life.location.changed",
                    "arguments": {},
                    "delivery": {
                        "mode": "webhook",
                        "url": "http://127.0.0.1/not-allowed",
                        "secret": "invalid",
                    },
                })
                self.assertEqual(status, 200)
                self.assertEqual(response["error"]["code"], -32602)
                # Invalid callback can't trigger any external POST.
                self.assertFalse((root / "subs.sqlite").stat().st_size == 0)
            finally:
                _stop(proxy)
                _stop(target)


if __name__ == "__main__":
    unittest.main()
