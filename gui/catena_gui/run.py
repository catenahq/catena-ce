"""The run directory: what a client answered, and where the install got to.

THE SINGLE BIGGEST STRUCTURAL REQUIREMENT, above any individual step. An
install contains two unbounded waits -- a server being delivered by a provider,
and a domain being activated by its registrar -- and neither fits inside a page
load or a shell session somebody is sitting in front of. So the answers live on
disk from the first one, and a launcher started again picks up where the last
one stopped.

WHAT IS ON DISK AND WHAT IS NOT. The answers file holds the non-secret ones,
0600 because "which server, which domain" is still a client's business. The
CREDENTIALS are held in memory for the life of the process and written only to
the transient adopt file the CLI already uses, which it deletes. A resumed run
asks for them again, which is the honest cost of not writing a client's cloud
credentials to their disk: the alternative is a file that outlives the install
and that nothing ever comes back to remove.

THE STATE IS A WORD AND A STEP, not a percentage. What a resumed run needs to
know is which page to open and whether the install already started, and both of
those are answerable. A progress figure would be a number nobody could act on.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# The states a run can be in. `installing` is the one that matters on resume: a
# launcher that reopened the form for a run whose `catena install` is still
# going would let a client answer the same questions twice into a host that is
# already being built.
STATE_ANSWERING = "answering"
STATE_INSTALLING = "installing"
STATE_DONE = "done"
STATE_FAILED = "failed"

ANSWERS_FILENAME = "answers.json"
LOG_FILENAME = "install.log"

@dataclass
class Run:
    """One install, resumable.

    `answers` is what a client typed and this file persists. `secrets` is what
    they typed that it does not, held for the life of the process only.

    `secret_keys` is the REGISTRY's answer to which is which, carried so the
    writer can enforce it rather than trusting every caller to have filtered.
    Classifying by NAME instead is wrong in both directions: `SSH_PRIVATE_KEY`
    holds a path and `_keyset_acknowledged` holds a yes, while a credential
    called `cloudflare_zone_proof` sails past every marker a heuristic carries.
    """

    path: Path
    answers: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    secret_keys: frozenset[str] = frozenset()
    state: str = STATE_ANSWERING
    step: str = ""
    inventory: str = ""
    started: float = 0.0

    @property
    def answers_path(self) -> Path:
        return self.path / ANSWERS_FILENAME

    @property
    def log_path(self) -> Path:
        return self.path / LOG_FILENAME

    def answer(self, key: str, value: str, *, secret: bool) -> None:
        """Record one answer, on the side of the line the REGISTRY puts it on."""
        if secret or key in self.secret_keys:
            self.secrets[key] = value
            return
        self.answers[key] = value

    def value(self, key: str) -> str:
        """What is currently answered for a key, from either side.

        One lookup, because a page rendering a field does not care which half
        of the run it came from -- only the writer does.
        """
        if key in self.secrets:
            return self.secrets[key]
        return self.answers.get(key, "")

    def missing_secrets(self, required: list[str]) -> list[str]:
        """Which credentials this run still has to be given.

        Non-empty on every resumed run that needs one, which is the point: the
        launcher asks again rather than pretending a value it deliberately did
        not keep is still available.
        """
        return [key for key in required if not (self.secrets.get(key) or "").strip()]

    def save(self) -> None:
        """Persist the non-secret half, 0600, atomically.

        0600 because which server and which domain is a client's business even
        though neither opens anything. Atomic because a launcher killed
        mid-write is exactly the case this file exists for, and a half-written
        answers file would resume into a form with some fields silently blank.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": self.state,
            "step": self.step,
            "inventory": self.inventory,
            "started": self.started or time.time(),
            "answers": {k: v for k, v in self.answers.items()
                        if k not in self.secret_keys},
        }
        tmp = self.answers_path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(self.answers_path))
        os.chmod(str(self.answers_path), 0o600)


def load(path: Path, secret_keys: frozenset[str] = frozenset()) -> Run:
    """Open a run directory, or start one.

    A directory that does not exist yet is a new run rather than an error: the
    launcher is started with a path and makes it, which is the same shape as
    being started twice with the same path.

    A malformed answers file IS an error. Resuming from one would silently drop
    whichever answers failed to parse, and a client would meet them again as
    blank fields with no explanation.

    The filter is applied on the way IN as well as on the way out. A file
    written by an older launcher, or edited by hand, is not a reason to load a
    credential into the answers half and then write it straight back.
    """
    run = Run(path=path, secret_keys=secret_keys)
    if not run.answers_path.is_file():
        return run
    doc = json.loads(run.answers_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{run.answers_path}: expected an object")
    run.state = str(doc.get("state") or STATE_ANSWERING)
    run.step = str(doc.get("step") or "")
    run.inventory = str(doc.get("inventory") or "")
    run.started = float(doc.get("started") or 0.0)
    answers = doc.get("answers")
    if isinstance(answers, dict):
        run.answers = {str(k): str(v) for k, v in answers.items()
                       if str(k) not in secret_keys}
    return run


def secret_keys_from(doc: dict) -> frozenset[str]:
    """Which keys the registry classifies as credentials."""
    return frozenset(entry["key"] for entry in (doc.get("secrets") or [])
                     if entry.get("key"))
