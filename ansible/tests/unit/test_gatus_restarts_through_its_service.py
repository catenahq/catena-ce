"""Gatus is restarted through its swarm service.

Gatus publishes its API on a host-mode port. `docker restart` of the task
container makes swarm see the task exit and start a replacement, which binds
the port first, so the restart fails with "Bind for 0.0.0.0:18080 failed:
port is already allocated" and the converge stops there. A service update
stops the old task before the new one binds.

Run: uv run pytest tests/unit/test_gatus_restarts_through_its_service.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "reconcile" / "roles" / "infrastructure" / "tasks" / "gatus.yml"
)


def _commands() -> list[tuple[str, str]]:
    out = []
    for task in yaml.safe_load(_TASKS.read_text()):
        for module in ("ansible.builtin.command", "ansible.builtin.shell"):
            spec = task.get(module)
            if spec is None:
                continue
            argv = spec.get("argv") if isinstance(spec, dict) else None
            out.append((task["name"], " ".join(argv) if argv else str(spec)))
    return out


def test_no_task_restarts_a_gatus_container():
    for name, cmd in _commands():
        assert "docker restart" not in cmd, (
            f"{name!r} restarts a container swarm owns: {cmd!r}")


def test_the_base_config_reload_updates_the_service():
    cmds = [cmd for _, cmd in _commands() if "service update" in cmd]
    assert cmds == ["docker service update --force --quiet {{ gatus_compose_name }}_app"]
