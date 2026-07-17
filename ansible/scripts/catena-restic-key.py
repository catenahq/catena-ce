#!/usr/bin/env python3
"""catena-restic-key -- validate or rotate the restic repository password.

Driven by catena-admin over the host dispatcher (as root): the admin container
base64-decodes a request into $PAYLOAD, the reserved action pipes it here on
stdin. The restic password is USER_HELD (never a settable settings field): this
is the ONLY on-box path that touches it after install, so a re-key is a
deliberate action with a scary confirm in the UI, not a passive settings save.

Request (JSON on stdin):
  {"op": "validate", "password": "<candidate>"}
  {"op": "rotate",   "new_password": "<new>"}

Reads the repo + S3 creds (and, for rotate, the CURRENT password) from the
on-box store /etc/catena/config.json. Prints a JSON result on stdout:
  validate -> {"ok": true|false}
  rotate   -> {"ok": true} on success (store + /etc/catena/restic.pass updated)

stdlib only (runs on the minimal host python); no PyYAML.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STORE_PATH = "/etc/catena/config.json"
PASS_FILE = "/etc/catena/restic.pass"


def _load_store(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"secrets": {}, "config": {}}
    raw = json.loads(p.read_text())
    return {"secrets": dict(raw.get("secrets") or {}),
            "config": dict(raw.get("config") or {})}


def _restic_env(store: dict) -> dict:
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = store["config"].get("BACKUP_RESTIC_REPO", "")
    env["AWS_ACCESS_KEY_ID"] = store["secrets"].get("vault_backup_s3_access_key", "")
    env["AWS_SECRET_ACCESS_KEY"] = store["secrets"].get("vault_backup_s3_secret_key", "")
    return env


def _write_secret_file(value: str) -> str:
    fd, name = tempfile.mkstemp(prefix="catena-restic-", suffix=".pass")
    os.write(fd, (value + "\n").encode())
    os.close(fd)
    os.chmod(name, 0o600)
    return name


def _opens(env: dict, password: str) -> bool:
    """Does `password` open the repo? `restic cat config` exits 0 iff yes."""
    pf = _write_secret_file(password)
    try:
        e = dict(env)
        e["RESTIC_PASSWORD_FILE"] = pf
        r = subprocess.run(["restic", "cat", "config"], env=e,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    finally:
        os.unlink(pf)


def cmd_validate(store: dict, req: dict) -> dict:
    password = str(req.get("password") or "")
    if not password:
        return {"ok": False, "error": "no password supplied"}
    if not store["config"].get("BACKUP_RESTIC_REPO"):
        return {"ok": False, "error": "backup repo not configured"}
    return {"ok": _opens(_restic_env(store), password)}


def cmd_rotate(store: dict, req: dict, store_path: str, pass_file: str) -> dict:
    new_password = str(req.get("new_password") or "")
    if not new_password:
        return {"ok": False, "error": "no new password supplied"}
    current = store["secrets"].get("vault_backup_restic_password", "")
    if not current:
        return {"ok": False, "error": "no current restic password on-box to re-key from"}
    env = _restic_env(store)
    if not env["RESTIC_REPOSITORY"]:
        return {"ok": False, "error": "backup repo not configured"}
    cur_pf = _write_secret_file(current)
    new_pf = _write_secret_file(new_password)
    try:
        env["RESTIC_PASSWORD_FILE"] = cur_pf
        # `restic key passwd` re-keys the CURRENT key: after this the repo opens
        # only with the new password. It authenticates with RESTIC_PASSWORD_FILE
        # (the current key) and sets the new one from --new-password-file.
        r = subprocess.run(
            ["restic", "key", "passwd", "--new-password-file", new_pf],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if r.returncode != 0:
            return {"ok": False, "error": "restic key passwd failed: "
                    + r.stderr.decode("utf-8", "replace")[:200]}
    finally:
        os.unlink(cur_pf)
        os.unlink(new_pf)

    # Persist the new password: the on-box store (rides the backup) + the
    # runtime pass file the backup service reads.
    store["secrets"]["vault_backup_restic_password"] = new_password
    tmp = store_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(store, indent=2, sort_keys=True).encode())
    finally:
        os.close(fd)
    os.replace(tmp, store_path)
    os.chmod(store_path, 0o600)
    Path(pass_file).write_text(new_password + "\n")
    os.chmod(pass_file, 0o600)
    return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    store_path = os.environ.get("CATENA_CONFIG_STORE", STORE_PATH)
    pass_file = os.environ.get("CATENA_RESTIC_PASS_FILE", PASS_FILE)
    req = json.loads(sys.stdin.read() or "{}")
    if not isinstance(req, dict):
        raise SystemExit("expected a JSON object on stdin")
    store = _load_store(store_path)
    op = req.get("op")
    if op == "validate":
        result = cmd_validate(store, req)
    elif op == "rotate":
        result = cmd_rotate(store, req, store_path, pass_file)
    else:
        raise SystemExit(f"unknown op {op!r}")
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
