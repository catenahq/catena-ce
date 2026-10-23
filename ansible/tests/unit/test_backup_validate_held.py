"""While the restore's hold record exists the backups wait for the destination,
so the backup validate reports a repository that does not open instead of
failing on it.

Run: uv run pytest tests/unit/test_backup_validate_held.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "backup"
TASKS = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())


def _index(name_fragment: str) -> int:
    for i, t in enumerate(TASKS):
        if name_fragment in (t.get("name") or ""):
            return i
    raise AssertionError(f"task not found: {name_fragment!r}")


def _when(task: dict, held: bool, rc: int) -> bool:
    """Evaluate the task's `when` list the way Ansible does (all must hold)."""
    env = jinja2.Environment()
    env.filters["bool"] = bool
    ctx = {
        "_bk_payload_expected": True,
        "_bk_configured": True,
        "_bk_held": {"stat": {"exists": held}},
        "_bk_restic_cat": {"rc": rc},
    }
    return all(
        env.compile_expression(expr)(**ctx) for expr in task["when"]
    )


def test_the_record_path_is_a_role_default():
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults["backup_held_record"] == "/var/lib/catena/backup-held.json"


def test_the_hold_stat_precedes_the_probe_under_its_conditions():
    stat = TASKS[_index("do the backups wait after a restore")]
    assert _index("do the backups wait after a restore") < _index("restic can reach the repo")
    assert stat["ansible.builtin.stat"]["path"] == "{{ backup_held_record }}"
    assert stat["register"] == "_bk_held"
    assert stat["when"] == TASKS[_index("restic can reach the repo")]["when"]


def test_the_assert_does_not_run_on_a_held_host():
    assert_task = TASKS[_index("restic cat config exited 0")]
    assert _when(assert_task, held=False, rc=124)
    assert not _when(assert_task, held=True, rc=124)


def test_the_assert_reads_the_one_reason():
    assert_task = TASKS[_index("restic cat config exited 0")]
    assert assert_task["ansible.builtin.assert"]["fail_msg"] == "{{ _bk_repo_reason }}"


def test_the_report_runs_only_when_held_and_the_probe_failed():
    report = TASKS[_index("the backups wait and the destination does not open")]
    assert _when(report, held=True, rc=124)
    assert not _when(report, held=True, rc=0)
    assert not _when(report, held=False, rc=124)


def _report(rc: int) -> str:
    reason = TASKS[_index("say why restic cat config")]["ansible.builtin.set_fact"]["_bk_repo_reason"]
    msg = TASKS[_index("the backups wait and the destination does not open")]["ansible.builtin.debug"]["msg"]
    ctx = {"_bk_restic_cat": {"rc": rc, "stderr": "boom"}}
    ctx["_bk_repo_reason"] = jinja2.Template(reason).render(**ctx)
    return jinja2.Template(msg).render(**ctx)


def test_the_report_carries_the_bound_sentence_and_the_no_repository_sentence():
    assert "did not answer within 120 seconds" in _report(124)
    assert "No restic repository exists" in _report(10)
    assert "backups wait after a restore" in _report(10)
