"""Shared SOPS+age vault operations for catena helpers.

Single point that shells out to `sops`. Any other script (helpers,
operator-tools, test_bench) that needs to read or write an
encrypted vault file should import from here instead of invoking
sops directly. Keeps the env handling, error wrapping, and CLI
flag conventions in one place.

All operations require the age private key in `SOPS_AGE_KEY`
(raw AGE-SECRET-KEY-1... content, not a file path). Per the
catena-tui -> Semaphore migration (M2), no private age key file
exists on any disk on any host: the operator pastes the key into
the calling shell at session start (`read -s SOPS_AGE_KEY &&
export SOPS_AGE_KEY`), where it materialises in process memory
only. SOPS_AGE_KEY_FILE is explicitly NOT consulted -- if it is
inherited from a parent shell it is stripped from forwarded env
so sops cannot quietly fall back to a stale on-disk key.

Missing/unset SOPS_AGE_KEY raises SopsError up-front instead of
letting sops fail with an opaque "no decryption key found" later.

The `cleartext-on-disk` exposure window is bounded:
  - read paths use `--output -` style (sops -d to stdout) so plaintext
    never lands in a file.
  - write paths use `--filename-override` + stdin so the file we write
    is already encrypted; the cleartext only exists in this Python
    process's memory.
  - in-place edits use `sops --set` so unchanged values keep their
    original IV/nonce (small, reviewable diffs).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

# The only env var we accept for the operator's age private key.
SOPS_AGE_KEY_ENV_VAR = "SOPS_AGE_KEY"
# Kept ONLY so we can defensively strip it from forwarded envs. We
# never read it; we only remove it, so a leaked SOPS_AGE_KEY_FILE in
# a parent shell cannot mask the SOPS_AGE_KEY we expect.
_SOPS_AGE_KEY_FILE_ENV_VAR = "SOPS_AGE_KEY_FILE"


class SopsError(RuntimeError):
    """Raised when sops returns non-zero, the key is missing, or the
    decrypted output cannot be parsed as YAML."""


def _ensure_age_key(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess env dict with the operator's age private key
    set in SOPS_AGE_KEY (raw content). Per the no-disk-key invariant,
    SOPS_AGE_KEY_FILE is NOT consulted; it is stripped from the
    forwarded env so an inherited stale path cannot mask the expected
    raw key.

    Raises SopsError up-front when SOPS_AGE_KEY is unset/empty so
    callers never see opaque sops decryption failures later."""
    e = dict(env if env is not None else os.environ)
    raw = e.get(SOPS_AGE_KEY_ENV_VAR) or os.environ.get(SOPS_AGE_KEY_ENV_VAR)
    # Always strip any inherited SOPS_AGE_KEY_FILE, even on the error
    # path -- callers expect the returned env to be hygienic.
    e.pop(_SOPS_AGE_KEY_FILE_ENV_VAR, None)
    if not raw:
        raise SopsError(
            f"{SOPS_AGE_KEY_ENV_VAR} is not set. Per the catena no-disk-key "
            "model, paste your age private key into the calling shell once "
            "per session:\n"
            f"  read -s {SOPS_AGE_KEY_ENV_VAR} && export {SOPS_AGE_KEY_ENV_VAR}\n"
            "(paste is hidden, no echo, no shell history with "
            "HISTCONTROL=ignorespace). Then re-run."
        )
    e[SOPS_AGE_KEY_ENV_VAR] = raw
    return e


def decrypt_text(path: Path, *, env: dict[str, str] | None = None) -> str:
    """Return the cleartext contents of a SOPS-encrypted YAML file.
    Plaintext is returned in-process; nothing touches disk."""
    e = _ensure_age_key(env)
    result = subprocess.run(
        ["sops", "-d", "--input-type", "yaml", "--output-type", "yaml", str(path)],
        capture_output=True, text=True, env=e, check=False,
    )
    if result.returncode != 0:
        raise SopsError(
            f"sops -d {path} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def read_dict(path: Path, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Decrypt and parse a SOPS-encrypted YAML file as a top-level dict.
    Raises SopsError if decryption fails or the YAML root is not a
    mapping."""
    text = decrypt_text(path, env=env)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SopsError(f"sops decrypted {path} but the YAML did not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise SopsError(
            f"sops decrypted {path} but the top-level value is "
            f"{type(data).__name__}, not a mapping."
        )
    return data


def read_value(
    path: Path,
    key: str,
    *,
    env: dict[str, str] | None = None,
    default: str = "",
) -> str:
    """Return data[key] from an encrypted vault as a string. Returns
    `default` when the file is unreadable, the key is absent, or the
    value is not a string. Use this when you only need one key and
    are OK swallowing decrypt failures (e.g. checking whether a key
    is present yet during an onboarding flow)."""
    try:
        data = read_dict(path, env=env)
    except SopsError:
        return default
    val = data.get(key)
    if isinstance(val, str):
        return val
    return default


def encrypt_text(
    cleartext: str,
    target_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Encrypt `cleartext` (YAML) and write it to `target_path`.
    Recipients come from the .sops.yaml whose creation_rule matches
    `target_path` (seed.py writes inventory/<inv>/.sops.yaml with the
    self age recipient). Cleartext is piped via stdin; sops's encrypted
    output is captured in-memory and only the encrypted bytes touch disk.

    sops discovers .sops.yaml by walking UP from its working directory
    (not from --filename-override, which only selects the creation_rule),
    so run it with cwd = the target's own directory. Otherwise `catena
    install` invoked from the repo root (e.g. the test bench) walks up
    from there, never reaches inventory/<inv>/.sops.yaml, and fails with
    "config file not found, or has no creation rules"."""
    e = _ensure_age_key(env)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "sops", "-e",
            "--input-type", "yaml",
            "--output-type", "yaml",
            "--filename-override", str(target_path),
            "/dev/stdin",
        ],
        input=cleartext, capture_output=True, text=True, env=e, check=False,
        cwd=str(target_path.parent),
    )
    if result.returncode != 0:
        raise SopsError(
            f"sops -e {target_path} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    target_path.write_text(result.stdout)
    target_path.chmod(0o600)


def encrypt_dict(
    data: dict[str, Any],
    target_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Convenience: yaml.safe_dump(data) + encrypt_text(...). Preserves
    insertion order via sort_keys=False and uses block style for
    readability."""
    cleartext = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    encrypt_text(cleartext, target_path, env=env)


def set_value(
    path: Path,
    key: str,
    value: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Set a single top-level key in an encrypted YAML vault, in place.
    Uses `sops --set` so unchanged values keep their original IV/nonce
    (the diff in git is minimal -- only the touched value re-encrypts).
    Creates the key if it does not already exist."""
    e = _ensure_age_key(env)
    set_arg = f'["{key}"] {json.dumps(value)}'
    result = subprocess.run(
        ["sops", "--set", set_arg, str(path)],
        capture_output=True, text=True, env=e, check=False,
    )
    if result.returncode != 0:
        raise SopsError(
            f"sops --set {key} on {path} failed "
            f"(rc={result.returncode}): {result.stderr.strip()}"
        )
