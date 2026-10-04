"""Paths into the ansible/ tree and readers of its YAML, shared by the unit
tests: a role by name, the panel's merged variables, a task list flattened,
a task found by name, and a converge's post_tasks with the shared tail in place.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE_ROOTS = (ANSIBLE / "bootstrap" / "roles", ANSIBLE / "reconcile" / "roles")
PLAYBOOKS = ANSIBLE / "playbooks"
GROUP_VARS = PLAYBOOKS / "group_vars" / "all" / "main.yml"
# The post_tasks converge.yml and reconcile.yml share.
CONVERGE_TAIL = "tasks/converge_tail.yml"


def role_dir(name: str) -> Path:
    """Where a role lives, whichever side it is on: found by search, so a role
    that moves across the boundary needs no test edited."""
    for root in ROLE_ROOTS:
        if (root / name).is_dir():
            return root / name
    raise AssertionError(f"no role named {name} under {[str(r) for r in ROLE_ROOTS]}")


# The bootstrap-side half of the panel: the runner account, the sudoers
# drop-in, the forced command.
PANEL_HOST_TASKS = role_dir("catena_admin_host") / "tasks" / "main.yml"


def panel_vars() -> dict:
    """Every variable the panel's converge reads, merged: group_vars (the
    values both of its halves need), then reconcile/roles/catena-admin and
    bootstrap/roles/catena_admin_host defaults. An assertion is then about the
    panel's configuration rather than about which file holds a line."""
    merged: dict = {}
    for path in (GROUP_VARS,
                 role_dir("catena-admin") / "defaults" / "main.yml",
                 role_dir("catena_admin_host") / "defaults" / "main.yml"):
        merged.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return merged


def walk_tasks(data) -> list[dict]:
    """Every task in a task list, descending into block / rescue / always."""
    out: list[dict] = []

    def _walk(items) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(item)
            for key in ("block", "rescue", "always"):
                _walk(item.get(key))

    _walk(data)
    return out


def task_index(tasks: list[dict], needle: str) -> int:
    """The position of the first task whose name contains `needle`."""
    for i, task in enumerate(tasks):
        if needle.lower() in str(task.get("name", "")).lower():
            return i
    raise AssertionError(
        f"no task matching {needle!r}; the ordering it anchors cannot be checked")


def command_text(task: dict) -> str:
    """What a command or shell task runs, however the module is spelled."""
    out: list[str] = []
    for key in ("ansible.builtin.command", "ansible.builtin.shell",
                "command", "shell"):
        mod = task.get(key)
        if isinstance(mod, str):
            out.append(mod)
        elif isinstance(mod, dict):
            argv = mod.get("argv")
            if isinstance(argv, list):
                out.append(" ".join(str(a) for a in argv))
            for f in ("cmd", "_raw_params"):
                if mod.get(f):
                    out.append(str(mod[f]))
    return "\n".join(out)


def post_tasks(playbook: Path) -> list[dict]:
    """A converge playbook's post_tasks, with the shared tail spliced in where
    it is included, each spliced task carrying the include's vars."""
    play = yaml.safe_load(playbook.read_text(encoding="utf-8"))[0]
    out: list[dict] = []
    for task in play.get("post_tasks") or []:
        include = task.get("ansible.builtin.include_tasks") or {}
        if include.get("file") != CONVERGE_TAIL:
            out.append(task)
            continue
        shared = task.get("vars") or {}
        for inner in yaml.safe_load((PLAYBOOKS / CONVERGE_TAIL).read_text(encoding="utf-8")):
            out.append({**inner, "vars": {**shared, **(inner.get("vars") or {})}})
    return out
