"""Both converge paths refuse a critically full disk, with one check.

converge.yml (the install) and reconcile.yml (the panel's Apply and the converge
timer) include playbooks/tasks/disk_preflight.yml before any role writes. The
ceiling and the message live in that file only, so the two paths cannot refuse
at different fullness or say different things.

Run: uv run pytest tests/unit/test_disk_preflight_both_converges.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

from ansible_tree import PLAYBOOKS, task_index, walk_tasks

_FILE = "tasks/disk_preflight.yml"
_GIB = 1073741824


def _play(name: str) -> dict:
    return yaml.safe_load((PLAYBOOKS / name).read_text(encoding="utf-8"))[0]


def _assert_task() -> dict:
    tasks = yaml.safe_load((PLAYBOOKS / _FILE).read_text(encoding="utf-8"))
    return tasks[task_index(tasks, "Preflight (disk)")]


def _render(template: str, **ctx) -> str:
    return jinja2.Environment().from_string(template).render(**ctx).strip()


def _refuses(used_pct: float, task: dict | None = None) -> str | None:
    """The fail message the assert renders for a mount that is `used_pct` full,
    or None when the mount passes."""
    task = task or _assert_task()
    mnt = {"mount": "/", "size_total": 100 * _GIB,
           "size_available": int(100 * _GIB * (100 - used_pct) / 100)}
    ctx = {**task["vars"], "mnt": mnt}
    ok = _render("{{ " + task["ansible.builtin.assert"]["that"][0].strip() + " }}", **ctx)
    if ok == "True":
        return None
    return " ".join(_render(task["ansible.builtin.assert"]["fail_msg"], **ctx).split())


def test_both_converges_include_the_shared_preflight_before_any_role():
    for name in ("converge.yml", "reconcile.yml"):
        play = _play(name)
        pre = play["pre_tasks"]
        idx = task_index(pre, "Preflight (disk)")
        include = pre[idx]["ansible.builtin.include_tasks"]
        assert include["file"] == _FILE, f"{name} does not include {_FILE}"
        assert "always" in pre[idx]["tags"], f"{name}: a --tags run would skip the refusal"
        assert "always" in include["apply"]["tags"], (
            f"{name}: the tasks inside the include would be filtered under --tags")
        assert play["roles"], f"{name} has no roles, so the order proves nothing"


def test_neither_playbook_carries_its_own_ceiling_or_assert():
    """One copy of the ceiling and the check: the playbooks hold only the include."""
    for name in ("converge.yml", "reconcile.yml"):
        text = (PLAYBOOKS / name).read_text(encoding="utf-8")
        assert "converge_disk_preflight" not in text, f"{name} has its own ceiling"
        own = [t for t in walk_tasks(_play(name)["pre_tasks"])
               if "Disk preflight" in str(t)]
        assert not own, f"{name} has its own disk assert"


def test_a_filesystem_at_or_above_the_ceiling_is_refused_below_it_passes():
    task = _assert_task()
    ceiling = task["vars"]["converge_disk_preflight_crit_pct"]
    assert ceiling == 90
    assert _refuses(ceiling - 0.5) is None
    msg = _refuses(ceiling)
    assert msg and msg.startswith("Disk preflight: / is 90.0% full")
    assert "-e skip_disk_preflight=true" in msg
    assert _refuses(99) is not None


def test_the_checked_mounts_are_the_root_and_the_data_volume():
    assert _assert_task()["vars"]["converge_disk_preflight_paths"] == ["/", "/srv/catena"]


def test_the_refusal_can_be_bypassed_only_by_the_named_variable():
    assert _assert_task()["when"] == "not (skip_disk_preflight | default(false) | bool)"
