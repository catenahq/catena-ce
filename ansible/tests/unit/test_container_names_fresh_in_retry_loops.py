"""A container name captured once must not be used inside a retry loop.

A swarm task is named <stack>_<service>.<slot>.<taskid> and the task id changes
on every reschedule, so a name resolved in one task is dead the moment swarm
replaces the task. test_mailserver_container_names_are_fresh.py pins reuse
across tasks; this pins reuse INSIDE a `retries:` loop, which exists precisely
to span the window in which a restart is most likely, so every attempt would
fail identically:

    Error response from daemon: No such container:
        keycloak_server.1.7dwol21sypwwm9n5kznb1anjh

The rule: if a task has `retries`, any container it names must be resolved
inside its own command.

Run: uv run pytest tests/unit/test_container_names_fresh_in_retry_loops.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ansible_tree import (ROLE_ROOTS as _ROLE_ROOTS, command_text as _command_text,
                          role_dir as _role_dir, walk_tasks as _tasks)

ANSIBLE = Path(__file__).resolve().parents[2]


# `docker ps`/`docker service ps` output is a NAME, and these are the registers
# the roles capture one into. A retried command interpolating one of these is
# reusing an answer that may already be stale.
_CAPTURED_NAME = re.compile(
    r"\{\{\s*_[A-Za-z0-9_]*(?:container|ct|_ps)[A-Za-z0-9_]*\.stdout"
)
_DOCKER_BY_NAME = re.compile(r"\bdocker\s+(?:exec|cp|inspect|logs|kill|stop)\b")




def _task_files() -> list[Path]:
    return sorted(p for root in _ROLE_ROOTS
                  for p in root.rglob("tasks/*.yml"))


def test_there_are_task_files_to_check() -> None:
    """Guard the guard: a layout change would make this vacuous."""
    files = _task_files()
    assert len(files) > 20, files


def test_no_retried_docker_command_reuses_a_captured_container_name() -> None:
    offenders: list[str] = []
    checked = 0
    for path in _task_files():
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for task in _tasks(data):
            if "retries" not in task:
                continue
            text = _command_text(task)
            if not _DOCKER_BY_NAME.search(text):
                continue
            checked += 1
            if _CAPTURED_NAME.search(text):
                offenders.append(
                    f"{path.relative_to(ANSIBLE)}: {task.get('name')!r} "
                    f"retries {task['retries']}x against a container name "
                    f"resolved in an earlier task"
                )
    assert checked, (
        "no retried docker-by-name command was found at all, so this test "
        "proves nothing. The roles moved or the task shape changed."
    )
    assert not offenders, (
        "a swarm task id changes on every reschedule, and a retry loop is "
        "exactly when that happens -- resolve the container inside the "
        "command:\n  " + "\n  ".join(offenders)
    )


def test_keycloak_health_probe_resolves_its_own_container() -> None:
    """The keycloak health probe: a retried command naming a container."""
    path = _role_dir("keycloak") / "tasks" / "validate.yml"
    probe = next(
        t for t in _tasks(yaml.safe_load(path.read_text()))
        if t.get("name", "").endswith("in-container /health/ready")
    )
    text = _command_text(probe)
    assert "docker ps" in text, (
        "the probe must look the container up itself on every attempt"
    )
    assert not _CAPTURED_NAME.search(text), text
