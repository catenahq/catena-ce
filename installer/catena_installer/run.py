"""The run: one inventory, what a client answered for it, and where the install
got to. What it keeps on disk, and what it never does, is in installer/README.md.

THE STATE IS A WORD, not a percentage. What a resumed run needs to know is
whether an install is running.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import tree

# The states a run can be in. `installing` is the one that matters on resume: a
# page that reopened the form for a run whose install is still going would let
# a client answer the same questions twice into a host that is already being
# built. Every other state takes answers and an install, which on an installed
# server converges and repairs it. The install runs in the installer process
# that started it, whose pid the state file records, so a run whose installer
# is gone reads as failed: its install went with it.
STATE_ANSWERING = "answering"
STATE_INSTALLING = "installing"
STATE_DONE = "done"
STATE_FAILED = "failed"

ENV_FILENAME = ".env"
STATE_FILENAME = ".catena-installer.json"
# How much of the install's output the page keeps, in a box that scrolls.
LOG_LINES = 2000


def seed():
    """seed.py, the one reader and writer of an inventory and its `.env`, so a
    file the page saved is byte-for-byte what the console would have written
    from the same answers."""
    return tree.seed()


@dataclass
class Run:
    """One inventory, resumable.

    `answers` is what a client typed and the `.env` keeps. `secrets` is what
    they typed that nothing keeps, held for the life of the process only.
    `keyset` is the passwords the install printed, by name, held for the life
    of the process too. `forward` is the panel account's forward this process
    holds open to the server, once the install has finished or the page asked
    for it.

    `secret_keys` is the REGISTRY's answer to which is which, carried so the
    writer can enforce it rather than trusting every caller to have filtered.
    """

    path: Path
    answers: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    secret_keys: frozenset[str] = frozenset()
    state: str = STATE_ANSWERING
    started: float = 0.0
    # The install's last lines and its passwords, in memory only.
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    keyset: dict[str, str] = field(default_factory=dict)
    forward: object | None = None

    @property
    def inventory(self) -> str:
        return self.path.name

    @property
    def env_path(self) -> Path:
        return self.path / ENV_FILENAME

    @property
    def state_path(self) -> Path:
        return self.path / STATE_FILENAME

    def answer(self, key: str, value: str, *, secret: bool) -> None:
        """Record one answer, on the side of the line the REGISTRY puts it on."""
        if secret or key in self.secret_keys:
            self.secrets[key] = value
        else:
            self.answers[key] = value

    def value(self, key: str) -> str:
        """What is currently answered for a key, from whichever half holds it."""
        for half in (self.secrets, self.answers):
            if key in half:
                return half[key]
        return ""

    def missing_secrets(self, required: list[str]) -> list[str]:
        """Which credentials this run still has to be given. Non-empty on every
        reopened inventory that needs one: the launcher asks again rather than
        pretending a value it deliberately did not keep is still available."""
        return [key for key in required if not (self.secrets.get(key) or "").strip()]

    def save(self) -> None:
        """Write the `.env` through seed's writer and the launcher's state, both
        0600, the state atomically. A credential never reaches the `.env`,
        whichever half a caller filed it in."""
        self.path.mkdir(parents=True, exist_ok=True)
        seed().write_env(self.path, {k: v for k, v in self.answers.items()
                                     if k not in self.secret_keys})

        payload = {
            "state": self.state,
            "started": self.started or time.time(),
            "pid": os.getpid(),
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(self.state_path))


def load(path: Path, secret_keys: frozenset[str] = frozenset()) -> Run:
    """Open an inventory, or start one.

    A directory that does not exist yet is a new run rather than an error. A
    malformed state file IS an error: resuming from one would silently drop the
    page and the install's state, and a client could answer into a host that is
    already being built.

    The `.env` is read with seed's reader. The filter is applied on the way IN
    as well as on the way out: a file edited by hand is not a reason to load a
    credential into the answers half and then write it straight back.
    """
    run = Run(path=path, secret_keys=secret_keys)
    if run.env_path.is_file():
        run.answers = {k: v for k, v in seed().read_existing_env(run.env_path).items()
                       if k not in secret_keys}
    if not run.state_path.is_file():
        return run
    doc = json.loads(run.state_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{run.state_path}: expected an object")
    run.state = str(doc.get("state") or STATE_ANSWERING)
    if run.state == STATE_INSTALLING and not _running(doc.get("pid")):
        run.state = STATE_FAILED
    run.started = float(doc.get("started") or 0.0)
    return run


def _running(pid: object) -> bool:
    """Whether the launcher process `pid` is still alive."""
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def secret_keys_from(doc: dict) -> frozenset[str]:
    """Which keys the registry classifies as credentials."""
    return frozenset(entry["key"] for entry in (doc.get("secrets") or [])
                     if entry.get("key"))
