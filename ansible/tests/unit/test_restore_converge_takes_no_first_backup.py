"""A whole-server restore converges without taking a first backup.

catena-admin payload/cmd/catena-recovery runs reconcile.yml with
--skip-tags edge,first_backup. After a restore from the offsite copy the
adopted store names a repository location that stays empty until the admin
puts the repository back from the copy, and a first backup that converge
queued would create a new repository there, which the put-back refuses.

So `first_backup` must reach every task that starts the backup unit, and the
snapshot probe the queue reads: skipped alone, the probe leaves the queue
reading no result as no snapshot. ansible-playbook accepts a skip tag nothing
carries, so a task outside it would run inside a restore without any error.

Run: uv run pytest tests/unit/test_restore_converge_takes_no_first_backup.py
"""
from __future__ import annotations

import yaml

from ansible_tree import ANSIBLE, PLAYBOOKS, ROLE_ROOTS, command_text, role_dir, walk_tasks

FIRST_BACKUP = "first_backup"
INSTALL = role_dir("backup") / "tasks" / "install.yml"
BACKUP_UNIT = ("{{ backup_systemd_unit_name }}", "catena-backup.service")


def _tags(task) -> set[str]:
    tags = task.get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _starts_the_backup_unit(task) -> bool:
    text = command_text(task)
    words = text.split()
    return ("systemctl" in words and "start" in words
            and any(unit in text for unit in BACKUP_UNIT))


def _unit_starters() -> dict[str, set[str]]:
    """Every task in the tree that starts the backup unit, by file and name,
    with its tags."""
    out: dict[str, set[str]] = {}
    for root in (*ROLE_ROOTS, PLAYBOOKS):
        for path in sorted(root.rglob("*.yml")):
            try:
                doc = yaml.safe_load(path.read_text())
            except yaml.YAMLError:
                continue
            for task in walk_tasks(doc):
                if _starts_the_backup_unit(task):
                    out[f"{path.relative_to(ANSIBLE)}: {task.get('name')}"] = _tags(task)
    return out


def test_the_probe_and_the_queue_carry_the_tag():
    tasks = walk_tasks(yaml.safe_load(INSTALL.read_text()))
    for fragment in ("Check whether any snapshots exist", "Run first backup inline"):
        found = [t for t in tasks if fragment in str(t.get("name", ""))]
        assert len(found) == 1, f"{fragment!r} matches {len(found)} task(s) in {INSTALL.name}"
        assert FIRST_BACKUP in _tags(found[0]), (
            f"{found[0]['name']!r} is outside `{FIRST_BACKUP}`, so a restore "
            "converge (--skip-tags edge,first_backup) runs it")


def test_nothing_a_restore_runs_starts_the_backup_unit():
    starters = _unit_starters()
    assert starters, "the scan found no task that starts the backup unit"
    outside = sorted(k for k, tags in starters.items() if FIRST_BACKUP not in tags)
    assert not outside, (
        "a restore converge (--skip-tags edge,first_backup) would start the "
        f"backup unit from these tasks; tag them `{FIRST_BACKUP}`: {outside}")
