"""The backup validate's "restic can reach the repo" task is bounded by
`timeout`, so an unreachable repository fails the assert after it with a
message naming the bound instead of waiting out restic's own retries.

Run: uv run pytest tests/unit/test_backup_validate_repo_probe_is_bounded.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

TASKS = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "backup" / "tasks" / "validate.yml"
)


def _find(name_fragment: str) -> dict:
    for t in yaml.safe_load(TASKS.read_text()):
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_repo_probe_runs_restic_under_timeout():
    """The entrypoint comes first (it execs its arguments with the repository
    credentials), so restic keeps the credentials off the command line and
    `timeout` sits between them."""
    argv = _find("restic can reach the repo")["ansible.builtin.command"]["argv"]
    assert argv[0] == "{{ backup_restic_env_script }}"
    assert argv[1] == "timeout"
    assert argv[-3:] == ["restic", "cat", "config"]
    assert any(a.isdigit() for a in argv[2:5])


def _fail_msg(rc: int) -> str:
    template = _find("restic cat config exited 0")["ansible.builtin.assert"]["fail_msg"]
    return jinja2.Template(template).render(_bk_restic_cat={"rc": rc, "stderr": "boom"})


def test_assert_names_the_bound_for_every_way_the_timeout_ends_the_command():
    """124 is the TERM, 137 is timeout after the KILL, -9 is the signal as
    Ansible reports it."""
    for rc in (124, 137, -9):
        assert "did not answer within 120 seconds" in _fail_msg(rc), rc


def test_assert_reports_other_failures_with_their_status():
    assert "rc=1" in _fail_msg(1) and "boom" in _fail_msg(1)
    assert "No restic repository exists" in _fail_msg(10)
