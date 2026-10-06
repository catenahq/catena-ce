"""Tell whether a server still presents the host key this machine trusts for it.

ssh refuses a changed host key, and from here a reinstalled server and another
machine answering at the same address look alike. Only the operator knows
which it is: `catena-cli install` asks, or takes `--reinstalled`, and the
graphical installer offers a box. On that answer `forget` drops the old key,
and bootstrap.yml's Phase 0 trusts the new one.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path

KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


def known_name(host: str, port: int = 22) -> str:
    """The name ssh files `host` under in known_hosts."""
    return host if port == 22 else f"[{host}]:{port}"


def _keys(lines: str) -> dict[tuple[str, str], str]:
    """(type, key) -> fingerprint, for each `name type key` line."""
    out = {}
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) < 3 or line.startswith(("#", "@")):
            continue
        digest = hashlib.sha256(base64.b64decode(parts[2])).digest()
        out[(parts[1], parts[2])] = (
            f"{parts[1]} SHA256:{base64.b64encode(digest).decode().rstrip('=')}")
    return out


def _trusted(host: str, port: int, known_hosts: Path) -> str:
    return subprocess.run(
        ["ssh-keygen", "-F", known_name(host, port), "-f", str(known_hosts)],
        capture_output=True, text=True).stdout


def _offered(host: str, port: int) -> str:
    return subprocess.run(
        ["ssh-keyscan", "-T", "10", "-p", str(port), host],
        capture_output=True, text=True).stdout


def changed(host: str, port: int = 22,
            known_hosts: Path = KNOWN_HOSTS) -> tuple[list[str], list[str]]:
    """The fingerprints this machine trusts for the server and the ones it
    offers, when it offers none of the trusted keys. Two empty lists when it
    offers one of them, when nothing is trusted for it yet, and when nothing
    answers."""
    trusted = _keys(_trusted(host, port, known_hosts)) if known_hosts.is_file() else {}
    if not trusted:
        return [], []
    offered = _keys(_offered(host, port))
    if not offered or trusted.keys() & offered.keys():
        return [], []
    return sorted(trusted.values()), sorted(offered.values())


def forget(host: str, port: int = 22, known_hosts: Path = KNOWN_HOSTS) -> None:
    """Drop every key trusted for the server."""
    subprocess.run(["ssh-keygen", "-R", known_name(host, port), "-f", str(known_hosts)],
                   capture_output=True, text=True, check=True)
