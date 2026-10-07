"""The SSH key pair the installer logs in with, and that ops and the panel
account accept: an existing pair from ~/.ssh, or one created with ssh-keygen,
which ships with OpenSSH on Windows 10 and later, macOS and Linux and sets the
file permissions OpenSSH checks on each of them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SSH_DIR = Path.home() / ".ssh"
DEFAULT_KEY = SSH_DIR / "catena_ed25519"


class KeyPairError(Exception):
    """A key pair that cannot be used or created."""


def pairs(ssh_dir: Path = SSH_DIR) -> list[Path]:
    """The private keys in `ssh_dir` that have their public half beside them."""
    if not ssh_dir.is_dir():
        return []
    return [pub.with_suffix("") for pub in sorted(ssh_dir.glob("*.pub"))
            if pub.with_suffix("").is_file()]


def create(private: Path, comment: str = "catena") -> None:
    """A new ed25519 pair at `private`, with no passphrase: the installer and
    the panel forward log in with it unattended."""
    if private.exists():
        raise KeyPairError(f"{private} already exists")
    if shutil.which("ssh-keygen") is None:
        raise KeyPairError("ssh-keygen is not installed: it ships with OpenSSH")
    private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    done = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,
         "-f", str(private)],
        capture_output=True, text=True)
    if done.returncode != 0:
        raise KeyPairError(f"ssh-keygen failed: {done.stderr.strip()}")


def public_line(private: Path) -> str:
    """The public key line beside `private`."""
    pub = private.with_name(private.name + ".pub")
    try:
        return pub.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError) as exc:
        raise KeyPairError(f"cannot read {pub}: {exc}") from exc
