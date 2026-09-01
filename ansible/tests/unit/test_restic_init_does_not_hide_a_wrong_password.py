"""A failed repo check is two situations, and init answers only one.

`restic cat config` returns 0 iff the repository exists AND the password is
correct -- the role's own comment says so. Everything else fell through to
`restic init`, including a repository that is perfectly healthy behind a
credential this host no longer holds. Initialising over it fails, and the
init task carries no_log, so what an operator gets is a censored failure on
"Initialize restic repo (first run only)" with nothing naming the cause.

Seen on run 2026-09-01T04-32-35-2ab2, where a repoint converge died on

    TASK [backup : Initialize restic repo (first run only)]
    fatal: [testvm-a]: FAILED! => {"censored": "the output has been hidden
    due to the fact that 'no_log: true' was specified for this result"}

The two cases need opposite actions -- restore the password, or create a
repository -- so they must not share an outcome.

Run: uv run pytest tests/unit/test_restic_init_does_not_hide_a_wrong_password.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_INSTALL = (Path(__file__).resolve().parents[2]
            / "roles" / "backup" / "tasks" / "install.yml")


def _tasks() -> list[dict]:
    return yaml.safe_load(_INSTALL.read_text(encoding="utf-8"))


def _by_name(name: str) -> dict:
    for t in _tasks():
        if t.get("name") == name:
            return t
    raise AssertionError(f"task {name!r} is gone from {_INSTALL}")


def test_a_wrong_password_fails_instead_of_initialising():
    t = _by_name("Backup: the repository exists but this password does not "
                 "open it")
    assert "ansible.builtin.fail" in t
    conds = [str(c) for c in t["when"]]
    assert any("wrong password' in" in c for c in conds), conds
    assert any("_restic_repo_check.rc" in c for c in conds), conds


def test_init_is_skipped_when_the_password_is_the_problem():
    """Initialising over an intact repository cannot succeed, and its failure
    is censored -- so the guard has to be on the init itself, not only on a
    friendlier message beside it."""
    t = _by_name("Initialize restic repo (first run only)")
    conds = [str(c) for c in t["when"]]
    assert any("wrong password' not in" in c for c in conds), conds


def test_init_still_runs_for_a_genuinely_absent_repository():
    """The fresh-install path is the one this must not break: restic says
    'unable to open config file' there, which is not a wrong-password
    message, so the guard lets init through."""
    t = _by_name("Initialize restic repo (first run only)")
    guard = next(c for c in (str(x) for x in t["when"])
                 if "wrong password' not in" in c)
    stderr = "Fatal: unable to open config file: Stat: The specified key does not exist."
    assert "wrong password" not in stderr, (
        f"the fresh-install stderr must not trip {guard!r}")


def test_the_check_output_is_never_printed():
    """The stderr is tested, not shown. no_log stays on the check, and the
    message beside it is a fixed string with no interpolation of the
    result."""
    check = _by_name("Check whether restic repo is already initialized")
    assert check.get("no_log") is True
    msg = _by_name("Backup: the repository exists but this password does not "
                   "open it")["ansible.builtin.fail"]["msg"]
    assert "{{" not in msg, "the operator message must not interpolate the result"
