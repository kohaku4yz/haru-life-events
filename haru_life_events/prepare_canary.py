"""Operator-only one-time canary preparation, never starts a daemon.

Builds a distinct *non-production* service configuration for the owner's
separate Tunnel. All generated secrets are local 0600 files; none go to Git,
stdout, ChatGPT, or the OpenAI tunnel control plane.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
from pathlib import Path

from cryptography.fernet import Fernet
from .settings import valid_owner

TUNNEL_ID = re.compile(r"^tunnel_[0-9a-f]{32}$")


def _private_directory(p: Path) -> None:
    if p.is_symlink():
        raise ValueError("configuration path cannot be symlink")
    if p.exists():
        st = p.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("directory must be user-owned and private")
    else:
        p.mkdir(mode=0o700, parents=True)
        p.chmod(0o700)


def _private_file(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        os.close(fd)


def prepare(
    *,
    tunnel_id: str,
    config_dir: Path,
    data_dir: Path,
    owner: str,
    port: int = 8794,
) -> dict[str, str]:
    if not TUNNEL_ID.fullmatch(tunnel_id):
        raise ValueError("invalid tunnel id")
    if not valid_owner(owner):
        raise ValueError("invalid owner label")
    if not isinstance(port, int) or not 1024 <= port < 65536:
        raise ValueError("invalid local port")
    if not config_dir.is_absolute() or not data_dir.is_absolute() or config_dir == data_dir:
        raise ValueError("separate absolute paths required")
    _private_directory(config_dir)
    _private_directory(data_dir)
    # Never regenerate secrets into an existing deployment.
    reserved = (
        "owner.key", "backend-auth-header.txt", "vault.key", "identity.key",
        "control-plane-runtime.key", "events.env", "tunnel.yaml"
    )
    if any((config_dir / name).exists() or (config_dir / name).is_symlink() for name in reserved):
        raise FileExistsError("canary already prepared; refusing to overwrite existing secrets")
    bearer = secrets.token_urlsafe(48).encode("ascii")
    _private_file(config_dir / "owner.key", bearer)
    _private_file(config_dir / "backend-auth-header.txt", b"Bearer " + bearer)
    _private_file(config_dir / "vault.key", Fernet.generate_key())
    _private_file(config_dir / "identity.key", secrets.token_bytes(48))
    # Empty placeholder, not a real credential. Readiness deliberately blocked
    # until the operator securely installs a dedicated new runtime key.
    _private_file(config_dir / "control-plane-runtime.key", b"")
    env = "\n".join([
        "HARU_EVENTS_BRIDGE_ENABLED=0",
        f"HARU_EVENTS_OWNER={owner}",
        f"HARU_EVENTS_API_KEY_FILE={config_dir / 'owner.key'}",
        f"HARU_EVENTS_FERNET_KEY_FILE={config_dir / 'vault.key'}",
        f"HARU_EVENTS_ID_KEY_FILE={config_dir / 'identity.key'}",
        f"HARU_EVENTS_SUBSCRIPTIONS_DB={data_dir / 'subscriptions.sqlite'}",
        f"HARU_EVENTS_LOOPBACK_PORT={port}",
        "",
    ])
    _private_file(config_dir / "events.env", env.encode())
    # Runtime key and backend bearer are intentionally separate!
    yaml = "\n".join([
        "config_version: 1",
        "control_plane:",
        '  base_url: "https://api.openai.com"',
        f'  tunnel_id: "{tunnel_id}"',
        f'  api_key: "file:{config_dir / "control-plane-runtime.key"}"',
        "health:",
        '  listen_addr: "127.0.0.1:0"',
        "admin_ui:",
        "  open_browser: false",
        "log:",
        "  level: info",
        "  format: json",
        "mcp:",
        "  server_urls:",
        "    - channel: main",
        f'      url: "http://127.0.0.1:{port}/mcp"',
        "  extra_headers:",
        f'    Authorization: "file:{config_dir / "backend-auth-header.txt"}"',
        "  discovery_extra_headers:",
        f'    Authorization: "file:{config_dir / "backend-auth-header.txt"}"',
        "",
    ])
    _private_file(config_dir / "tunnel.yaml", yaml.encode())
    return {
        "config_dir": str(config_dir),
        "data_dir": str(data_dir),
        "tunnel_id": tunnel_id,
        "owner": owner,
        "backend_port": str(port),
        "enabled": "false",
        "control_plane_key_installed": "false",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare private Haru Events canary, OFF by default.")
    parser.add_argument("--tunnel-id", required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--port", type=int, default=8794)
    args = parser.parse_args()
    import json
    result = prepare(**vars(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
