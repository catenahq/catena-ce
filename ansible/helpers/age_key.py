"""Mint / derive the self-hoster's age key for the SOPS vault (Community).

Community Catena is single-tenant self-hosted: there is no operator and no
client handover. The vault is encrypted to ONE age recipient -- the
self-hoster's own key. This module mints that key on first install and
derives the public key (the single recipient embedded in the
per-inventory .sops.yaml) from either a freshly minted key, an existing
key file, or the raw SOPS_AGE_KEY content already in the environment.

Shells out to `age-keygen` (the same toolchain sops and the
community.sops Ansible vars plugin already depend on), so a missing
prerequisite surfaces early with a useful message rather than an opaque
decryption failure later. The private key never lands on disk except in
the user-owned key file written by the installer on first run.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

# `age-keygen` (mint) prints:
#   # created: <RFC3339>
#   # public key: age1...
#   AGE-SECRET-KEY-1...
# `age-keygen -y <file>` (derive) prints a bare `age1...` line.
_PUBKEY_BARE_RE = re.compile(r"^(age1[a-z0-9]+)\s*$", re.MULTILINE)
_PUBKEY_COMMENT_RE = re.compile(r"^# public key:\s*(age1[a-z0-9]+)\s*$", re.MULTILINE)
_PRIVKEY_RE = re.compile(r"^(AGE-SECRET-KEY-1[A-Z0-9]+)\s*$", re.MULTILINE)


class AgeKey(NamedTuple):
    pubkey: str
    privkey: str


class AgeKeyError(RuntimeError):
    pass


def _require_age_keygen() -> None:
    if shutil.which("age-keygen") is None:
        raise AgeKeyError(
            "age-keygen not on PATH. Install age "
            "(https://github.com/FiloSottile/age) -- it is the same "
            "toolchain sops uses for vault encryption."
        )


def mint() -> AgeKey:
    """Mint a fresh age keypair and return both halves in memory."""
    _require_age_keygen()
    result = subprocess.run(
        ["age-keygen"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AgeKeyError(
            f"age-keygen failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return parse_keygen_output(result.stdout)


def parse_keygen_output(text: str) -> AgeKey:
    """Parse `age-keygen` stdout into (pubkey, privkey). Factored out so
    the parser is unit-testable without invoking the real binary."""
    pub = _PUBKEY_COMMENT_RE.search(text)
    priv = _PRIVKEY_RE.search(text)
    if not pub or not priv:
        raise AgeKeyError(
            "age-keygen output did not match expected format (missing "
            f"'# public key:' or 'AGE-SECRET-KEY-1' line). Raw output: {text!r}"
        )
    return AgeKey(pubkey=pub.group(1), privkey=priv.group(1))


def extract_secret(text: str) -> str | None:
    """Return the AGE-SECRET-KEY-1 line from a key-file body, or None."""
    m = _PRIVKEY_RE.search(text)
    return m.group(1) if m else None


def pubkey_from_secret(secret: str) -> str:
    """Derive the age public key from a raw AGE-SECRET-KEY-1 string.

    Writes the secret to a 0600 tempfile (mkstemp already creates it that
    way) so `age-keygen -y` can read it, then removes it. The secret only
    exists on disk for the duration of the derive call."""
    _require_age_keygen()
    with tempfile.NamedTemporaryFile("w", suffix=".key", delete=True) as tf:
        tf.write(secret.strip() + "\n")
        tf.flush()
        result = subprocess.run(
            ["age-keygen", "-y", tf.name],
            capture_output=True, text=True, check=False,
        )
    if result.returncode != 0:
        raise AgeKeyError(
            f"age-keygen -y failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    m = _PUBKEY_BARE_RE.search(result.stdout)
    if not m:
        raise AgeKeyError(
            f"age-keygen -y produced no age1 public key. Output: {result.stdout!r}"
        )
    return m.group(1)


def keyfile_body(key: AgeKey) -> str:
    """Render a standard age key-file body (comments + secret line)."""
    return (
        "# created by catena installer\n"
        f"# public key: {key.pubkey}\n"
        f"{key.privkey}\n"
    )


def pubkey_from_file(path: Path) -> str:
    """Derive the age public key from an existing key file."""
    _require_age_keygen()
    result = subprocess.run(
        ["age-keygen", "-y", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise AgeKeyError(
            f"age-keygen -y {path} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    m = _PUBKEY_BARE_RE.search(result.stdout)
    if not m:
        raise AgeKeyError(
            f"age-keygen -y {path} produced no age1 public key. "
            f"Output: {result.stdout!r}"
        )
    return m.group(1)
