#!/usr/bin/env python3
"""catena-restic-key -- generate, validate or rotate the restic repository
password, and create the repository at the saved destination.

Driven by catena-admin over the host dispatcher (as root): the admin container
base64-decodes a request into $PAYLOAD, the reserved action pipes it here on
stdin. The restic password is USER_HELD (never a settable settings field) and
no converge mints it: this is the ONLY on-box path that writes it, so a
generation or a re-key is a deliberate action in the UI, not a passive
settings save.

It is also the one place a repository is created. A backup never creates one:
saving the destination in the panel asks for `init`, and so does generating the
password for a destination saved before it.

While the backups wait after a whole-server restore (the record catena-admin
payload/engines/backuphold writes), the repository is not re-keyed, and a new
destination saved in the panel gets a new password in place of the stored one
(`generate` with `replace`), and so does the destination the restore adopted
when the hold records the Restore page's choice of a fresh repository.

Request (JSON on stdin):
  {"op": "generate"}
  {"op": "generate", "replace": true}
  {"op": "validate", "password": "<candidate>"}
  {"op": "rotate",   "new_password": "<new>"}
  {"op": "init"}

Reads the repo + S3 creds (and, for rotate and init, the CURRENT password) from
the on-box store /etc/catena/config.json. Prints a JSON result on stdout:
  generate -> {"ok": true, "password": "<new>"} once, for the client to save
  validate -> {"ok": true|false}
  rotate   -> {"ok": true} on success (store + /etc/catena/restic.pass updated)
  init     -> {"ok": true, "repository": "created"|"exists"}

stdlib only (runs on the minimal host python); no PyYAML.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STORE_PATH = "/etc/catena/config.json"
PASS_FILE = "/etc/catena/restic.pass"
HELD_FILE = "/var/lib/catena/backup-held.json"


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
    env["AWS_ACCESS_KEY_ID"] = store["secrets"].get("backup_s3_access_key", "")
    env["AWS_SECRET_ACCESS_KEY"] = store["secrets"].get("backup_s3_secret_key", "")
    return env


def _write_secret_file(value: str) -> str:
    fd, name = tempfile.mkstemp(prefix="catena-restic-", suffix=".pass")
    os.write(fd, (value + "\n").encode())
    os.close(fd)
    os.chmod(name, 0o600)
    return name


# `restic cat config` exit codes: the repository does not exist, or the
# password does not open it.
_NO_REPOSITORY = 10
_WRONG_PASSWORD = 12


def _cat_config(env: dict, password: str) -> int:
    """`restic cat config` with `password`; its exit code. --no-lock: the
    question writes nothing into the repository it asks."""
    pf = _write_secret_file(password)
    try:
        e = dict(env)
        e["RESTIC_PASSWORD_FILE"] = pf
        return subprocess.run(["restic", "cat", "config", "--no-lock"], env=e,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    finally:
        os.unlink(pf)


def _opens(env: dict, password: str) -> bool:
    """Does `password` open the repo? `restic cat config` exits 0 iff yes."""
    return _cat_config(env, password) == 0


def _save_store(store: dict, store_path: str) -> None:
    """Write the two sections back, keeping every other top-level key the
    file holds (schedules, image pins, offsite copies): other writers own
    those."""
    p = Path(store_path)
    doc = json.loads(p.read_text()) if p.exists() else {}
    doc["secrets"] = store["secrets"]
    doc["config"] = store["config"]
    tmp = store_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(doc, indent=2, sort_keys=True).encode())
    finally:
        os.close(fd)
    os.replace(tmp, store_path)
    os.chmod(store_path, 0o600)


def _held(path: str) -> dict | None:
    """The record that the backups wait after a whole-server restore, or None
    while they do not. A record that cannot be read holds them all the same."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def cmd_generate(store: dict, store_path: str, replace: bool = False,
                 held_file: str = HELD_FILE, pass_file: str = PASS_FILE) -> dict:
    """Mint the password when the store has none, and keep it.

    A repository that already holds backups under another password stays
    theirs: a new password would make every backup to it fail, and a server
    continuing on an earlier server's repository is a restore of it.

    With `replace`, the password minted takes the stored one's place: only
    while the backups wait after a restore, for a destination other than the
    one the restore adopted, which holds no repository yet. The old password
    may be known to whoever compromised the server before the restore.

    The adopted destination itself gets a new password only when the Restore
    page's choice recorded in the hold is "fresh": the repository there is
    re-keyed to it (the stored password has to open it)."""
    if store["secrets"].get("backup_restic_password"):
        if not replace:
            return {"ok": False, "error": "a backup encryption password already "
                    "exists; validate or rotate it instead"}
        hold = _held(held_file)
        if hold is None:
            return {"ok": False, "error": "a new backup encryption password "
                    "replaces the stored one only while the backups wait after "
                    "a restore; rotate it instead"}
        repo = store["config"].get("BACKUP_RESTIC_REPO", "")
        here = _location(repo)
        if here is None:
            return {"ok": False, "error": "the backup destination is not a "
                    "repository address; save one in Settings"}
        if here == _location(str(hold.get("settings") or "")):
            if hold.get("choice") != "fresh":
                return {"ok": False, "error": "the backup destination is the "
                        "one the restore brought back; a new password is made "
                        "for a new destination, or when the Restore page's "
                        "choice is a fresh repository"}
            password = base64.b64encode(os.urandom(48)).decode("ascii")
            result = _set_password(store, password, store_path, pass_file)
            return {"ok": True, "password": password} if result["ok"] else result
    password = base64.b64encode(os.urandom(48)).decode("ascii")
    env = _restic_env(store)
    if env["RESTIC_REPOSITORY"]:
        rc = _cat_config(env, password)
        if rc == _WRONG_PASSWORD:
            return {"ok": False, "error": "the repository already holds backups "
                    "encrypted under another password. Restore this server "
                    "from it on the Restore page, or point Settings at an "
                    "empty repository"}
        if rc != _NO_REPOSITORY:
            return {"ok": False, "error": f"could not read the repository "
                    f"(restic exit {rc}); check its address and keys"}
    store["secrets"]["backup_restic_password"] = password
    _save_store(store, store_path)
    if replace:
        Path(pass_file).write_text(password + "\n")
        os.chmod(pass_file, 0o600)
    return {"ok": True, "password": password}


def _location(repo: str) -> tuple[str, str, str] | None:
    """An s3:[<scheme>://]<endpoint>/<bucket>[/<prefix>] repository as its
    scheme, endpoint host and bucket path, the grammar catena-admin
    payload/engines/s3repo reads; a bare endpoint is https, as it is to restic.
    None for anything else."""
    if not repo.startswith("s3:"):
        return None
    rest = repo[len("s3:"):]
    scheme = "https"
    for prefix in ("http://", "https://"):
        if rest.startswith(prefix):
            scheme, rest = prefix[:-len("://")], rest[len(prefix):]
    host, _, path = rest.partition("/")
    path = path.strip("/")
    if not host or not path:
        return None
    return scheme, host, path


def _rclone_location(env: dict) -> tuple[dict, str] | None:
    """The repository's location as an rclone remote: the process env with a
    DEST remote defined against its endpoint and keys, and the
    dest:<bucket>[/<prefix>] path. None for anything _location does not
    read."""
    location = _location(env["RESTIC_REPOSITORY"])
    if location is None:
        return None
    scheme, host, path = location
    renv = dict(env)
    renv.update({
        "RCLONE_CONFIG_DEST_TYPE": "s3",
        "RCLONE_CONFIG_DEST_PROVIDER": "Other",
        "RCLONE_CONFIG_DEST_ENDPOINT": f"{scheme}://{host}",
        "RCLONE_CONFIG_DEST_ACCESS_KEY_ID": env["AWS_ACCESS_KEY_ID"],
        "RCLONE_CONFIG_DEST_SECRET_ACCESS_KEY": env["AWS_SECRET_ACCESS_KEY"],
    })
    return renv, "dest:" + path


# rclone's exit code for a directory it does not find: on S3, a bucket that
# does not exist yet, which `restic init` creates.
_RCLONE_DIR_NOT_FOUND = 3


def _location_holds_anything(env: dict) -> bool | None:
    """Whether the repository's location holds any object, or None when it
    could not be listed. The first entry answers, so a bucket holding an
    application's millions of objects is not listed whole."""
    location = _rclone_location(env)
    if location is None:
        return None
    renv, path = location
    with subprocess.Popen(["rclone", "lsf", path], env=renv, text=True,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as proc:
        if proc.stdout.readline().strip():
            proc.kill()
            return True
        rc = proc.wait()
    if rc in (0, _RCLONE_DIR_NOT_FOUND):
        return False
    return None


def cmd_init(store: dict) -> dict:
    """Create the repository at the saved destination when it holds none.

    A repository the stored password opens is left as it is. A destination
    holding anything else is refused: another password's repository is a
    restore waiting to happen, and a repository created beside other files
    shares its location with whatever deletes or replaces them."""
    env = _restic_env(store)
    password = store["secrets"].get("backup_restic_password", "")
    if not (env["RESTIC_REPOSITORY"] and env["AWS_ACCESS_KEY_ID"]
            and env["AWS_SECRET_ACCESS_KEY"]):
        return {"ok": False, "error": "no backup destination is saved"}
    if not password:
        return {"ok": False, "error": "no backup encryption password is "
                "stored yet; generate it on the Settings page"}
    rc = _cat_config(env, password)
    if rc == 0:
        return {"ok": True, "repository": "exists"}
    if rc == _WRONG_PASSWORD:
        return {"ok": False, "error": "the destination holds a repository "
                "encrypted under another password. Restore this server from "
                "it on the Restore page, or point Settings at an empty bucket"}
    if rc != _NO_REPOSITORY:
        return {"ok": False, "error": f"could not read the destination "
                f"(restic exit {rc}); check its address and keys"}
    holds = _location_holds_anything(env)
    if holds is None:
        return {"ok": False, "error": "could not list the destination, so "
                "nothing was created there; check its address and keys"}
    if holds:
        return {"ok": False, "error": "the destination holds files that are "
                "not a restic repository. Point Settings at an empty bucket, "
                "or at a prefix nothing else uses"}
    pf = _write_secret_file(password)
    try:
        e = dict(env)
        e["RESTIC_PASSWORD_FILE"] = pf
        r = subprocess.run(["restic", "init"], env=e,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    finally:
        os.unlink(pf)
    if r.returncode != 0:
        return {"ok": False, "error": "restic init failed: "
                + r.stderr.decode("utf-8", "replace")[:200]}
    return {"ok": True, "repository": "created"}


def cmd_validate(store: dict, req: dict) -> dict:
    password = str(req.get("password") or "")
    if not password:
        return {"ok": False, "error": "no password supplied"}
    if not store["config"].get("BACKUP_RESTIC_REPO"):
        return {"ok": False, "error": "backup repo not configured"}
    return {"ok": _opens(_restic_env(store), password)}


def cmd_rotate(store: dict, req: dict, store_path: str, pass_file: str,
               held_file: str = HELD_FILE) -> dict:
    if _held(held_file) is not None:
        return {"ok": False, "error": "the backups wait after a restore of this "
                "server, and nothing is written to the repository until the "
                "destination is confirmed, or a new one saved, in Settings"}
    new_password = str(req.get("new_password") or "")
    if not new_password:
        return {"ok": False, "error": "no new password supplied"}
    return _set_password(store, new_password, store_path, pass_file)


def _set_password(store: dict, new_password: str, store_path: str,
                  pass_file: str) -> dict:
    """Re-key the repository the store names from the stored password to
    new_password, then persist it in the store and the runtime pass file."""
    current = store["secrets"].get("backup_restic_password", "")
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
    store["secrets"]["backup_restic_password"] = new_password
    _save_store(store, store_path)
    Path(pass_file).write_text(new_password + "\n")
    os.chmod(pass_file, 0o600)
    return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    store_path = os.environ.get("CATENA_CONFIG_STORE", STORE_PATH)
    pass_file = os.environ.get("CATENA_RESTIC_PASS_FILE", PASS_FILE)
    held_file = os.environ.get("CATENA_BACKUP_HELD_FILE", HELD_FILE)
    req = json.loads(sys.stdin.read() or "{}")
    if not isinstance(req, dict):
        raise SystemExit("expected a JSON object on stdin")
    store = _load_store(store_path)
    op = req.get("op")
    if op == "generate":
        result = cmd_generate(store, store_path, req.get("replace") is True,
                              held_file, pass_file)
    elif op == "validate":
        result = cmd_validate(store, req)
    elif op == "rotate":
        result = cmd_rotate(store, req, store_path, pass_file, held_file)
    elif op == "init":
        result = cmd_init(store)
    else:
        raise SystemExit(f"unknown op {op!r}")
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
