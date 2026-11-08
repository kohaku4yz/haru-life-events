"""Canary: tunnel-client injects private backend auth on final MCP hop.

Requires HARU_TUNNEL_DEV_PROXY_BIN=/path/to/tunnel-client and uses only
temporary secret files, localhost, fake tunnel ID/in-memory control plane.
No actual connection, tokens or external callbacks.
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


def choose_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(proc):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=2)


@unittest.skipUnless(os.getenv("HARU_TUNNEL_DEV_PROXY_BIN"), "requires tunnel-client binary")
class TestPrivateBackendHeader(unittest.TestCase):
    def test_no_plugin_bearer_required_with_private_tunnel_header(self):
        binary = os.environ["HARU_TUNNEL_DEV_PROXY_BIN"]
        with tempfile.TemporaryDirectory(prefix="haru-static-mcp-auth-") as td:
            root = Path(td)
            token = b"Z" * 48
            for name, data in [
                ("owner.key", token),
                ("vault.key", Fernet.generate_key()),
                ("identity.key", b"y" * 40),
                ("backend-header.txt", b"Bearer " + token),
            ]:
                f = root / name
                f.write_bytes(data)
                f.chmod(0o600)
            p = choose_port()
            profile = root / "dev-profile.yaml"
            profile.write_text(
                "config_version: 1\n"
                "control_plane:\n"
                "  base_url: https://api.openai.com\n"
                "  tunnel_id: tunnel_22222222222222222222222222222222\n"
                "  api_key: env:NOT_SET_IN_THIS_FAKE_TEST\n"
                "health:\n"
                "  listen_addr: 127.0.0.1:0\n"
                "mcp:\n"
                "  server_urls:\n"
                "    - channel: main\n"
                f"      url: http://127.0.0.1:{p}/mcp\n"
                "  extra_headers:\n"
                f"    Authorization: file:{root / 'backend-header.txt'}\n"
                "  discovery_extra_headers:\n"
                f"    Authorization: file:{root / 'backend-header.txt'}\n"
            )
            profile.chmod(0o600)
            env = dict(os.environ)
            env.update(
                HARU_EVENTS_BRIDGE_ENABLED="1",
                HARU_EVENTS_OWNER="single-test-owner",
                HARU_EVENTS_API_KEY_FILE=str(root / "owner.key"),
                HARU_EVENTS_FERNET_KEY_FILE=str(root / "vault.key"),
                HARU_EVENTS_ID_KEY_FILE=str(root / "identity.key"),
                HARU_EVENTS_SUBSCRIPTIONS_DB=str(root / "subs.sqlite"),
                HARU_EVENTS_LOOPBACK_PORT=str(p),
                NOT_SET_IN_THIS_FAKE_TEST="not-a-real-control-plane-key",
            )
            target = proxy = None
            try:
                target = subprocess.Popen(
                    [sys.executable, "-m", "haru_life_events.tunnel_events_local"],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                end = time.monotonic() + 7
                while time.monotonic() < end:
                    if target.poll() is not None:
                        self.fail("isolated MCP server exited")
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{p}/mcp", timeout=0.2)
                    except urllib.error.HTTPError as exc:
                        if exc.code in (401, 404):
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                result_path = root / "proxy.json"
                proxy_errors = (root / "proxy-errors.log").open("wb")
                proxy = subprocess.Popen(
                    [binary, "dev", "proxy", "--profile-file", str(profile),
                     "--listen", "127.0.0.1:0",
                     "--url-file", str(result_path),
                     "--duration", "23s", "--readiness-timeout", "6s",
                     "--response-timeout", "8s"],
                    env=env, stdout=subprocess.DEVNULL, stderr=proxy_errors,
                )
                metadata = None
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    if result_path.exists():
                        try:
                            metadata = json.loads(result_path.read_text())
                        except (ValueError, OSError):
                            pass
                        if metadata:
                            break
                    if proxy.poll() is not None:
                        break
                    time.sleep(0.06)
                if metadata is None:
                    proxy_errors.flush()
                    debug = (root / "proxy-errors.log").read_text(errors="replace")[-700:]
                    debug = debug.replace("Z" * 8, "[REDACTED]")
                    self.fail("tunnel-client dev proxy not ready; exit=" + str(proxy.poll()) + " log=" + debug)
                proxy_errors.close()
                endpoint = metadata["mcp_url"]
                parsed = urlsplit(endpoint)
                self.assertEqual(parsed.hostname, "127.0.0.1")

                def rpc(method, *, client_auth=None):
                    payload = {
                        "jsonrpc": "2.0", "id": 2, "method": method,
                        "params": {"_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }},
                    }
                    headers = {
                        "content-type": "application/json",
                        "Mcp-Method": method,
                        "MCP-Protocol-Version": "2026-07-28",
                    }
                    if client_auth:
                        headers["Authorization"] = client_auth
                    request = urllib.request.Request(
                        endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST"
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=8) as resp:
                            return resp.status, json.load(resp)
                    except urllib.error.HTTPError as err:
                        return err.code, json.loads(err.read(4096))

                status, response = rpc("server/discover")
                self.assertEqual(status, 200)
                self.assertEqual(response["result"]["supportedVersions"], ["2026-07-28"])
                status, response = rpc("events/list")
                self.assertEqual(status, 200)
                self.assertIn("life.location.changed", {
                    item["name"] for item in response["result"]["events"]
                })
                # Static tunnel headers may be overridden by a forwarded client header.
                status, _ = rpc("events/list", client_auth="Bearer " + "B" * 48)
                self.assertEqual(status, 401)
                # Direct access to the local service never works without the token.
                unauthenticated = urllib.request.Request(
                    f"http://127.0.0.1:{p}/mcp",
                    data=json.dumps({"jsonrpc":"2.0", "id":1, "method":"events/list"}).encode(),
                    headers={"Content-Type":"application/json"}, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(unauthenticated, timeout=1)
                self.assertEqual(raised.exception.code, 401)
            finally:
                stop(proxy)
                stop(target)


if __name__ == "__main__":
    unittest.main()
