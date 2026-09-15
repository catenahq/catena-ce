"""Swarm keeps one EXITED container per retained task record.

Reported as "gatus v5.36.0 has 5 container instances, of which only 1 is
running", guessed to be the random id docker appends to a stack's container
names creating duplicates. Five is Docker's default `--task-history-limit`:
swarm keeps five task records per service and one container each, so a service
on a converged host shows five containers with one running, and the number is
the limit rather than a symptom of anything.

The two readings look identical from `docker ps -a` and are told apart by
`docker service ps <svc> --no-trunc`: "Shutdown / Complete" rows are ordinary
rolls, "Failed / task: non-zero exit" rows are a crash loop.

Run: uv run pytest tests/unit/test_swarm_task_history.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
DOCKER_TASKS = ANSIBLE / "bootstrap" / "roles" / "docker" / "tasks" / "main.yml"
DOCKER_DEFAULTS = ANSIBLE / "bootstrap" / "roles" / "docker" / "defaults" / "main.yml"


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(DOCKER_TASKS.read_text()) if isinstance(t, dict)]


def _by_name(fragment: str) -> dict:
    return next(t for t in _tasks() if fragment in str(t.get("name", "")))


def test_the_limit_is_set_and_is_not_dockers_default():
    defaults = yaml.safe_load(DOCKER_DEFAULTS.read_text())
    limit = defaults.get("docker_swarm_task_history_limit")
    assert limit is not None, (
        "nothing sets the task-history limit; Docker's default of 5 is what "
        "left five containers per service on the host")
    assert limit != 5, "setting the limit to Docker's own default changes nothing"


def test_the_limit_still_keeps_the_previous_task():
    """At 1 or 0, `docker service ps` shows only the task running now: a
    service that just recovered has no record of what it recovered from, and a
    crash loop reads the same as a healthy first boot. The history exists to
    answer that question."""
    defaults = yaml.safe_load(DOCKER_DEFAULTS.read_text())
    assert defaults["docker_swarm_task_history_limit"] >= 2


def test_the_update_is_gated_on_a_read_not_run_every_converge():
    """`docker swarm update` exits 0 and prints "Swarm updated." whether or not
    anything changed, so an ungated task reports changed on every converge --
    which fails the bench's strict changed=0 idempotency rerun and, worse,
    trains the reader to ignore a changed line."""
    task = _by_name("bound task history")
    when = str(task.get("when"))
    assert "_docker_task_history" in when, (
        "the update does not compare against the limit currently in force")
    assert "docker_swarm_task_history_limit" in when


def test_the_read_cannot_fail_the_play():
    """An older dockerd, or one whose info output drops the field, must leave
    the host converging rather than abort it over a retention setting."""
    read = _by_name("read the task-history retention limit")
    assert read.get("failed_when") is False
    assert read.get("changed_when") is False


def test_the_limit_is_set_after_the_swarm_exists():
    """`docker swarm update` exits non-zero unless the node is a manager, and
    the init above is what makes it one."""
    names = [str(t.get("name", "")) for t in _tasks()]
    init = next(i for i, n in enumerate(names) if "init single-node swarm" in n)
    bound = next(i for i, n in enumerate(names) if "bound task history" in n)
    assert init < bound, names[init:bound + 1]
