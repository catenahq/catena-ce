"""A swarm stack deploy is bounded, and hitting the bound during a pull retries.

`docker stack deploy --detach=false` blocks until every task converges. A task
that CANNOT converge -- an image the daemon is unable to pull -- never lets it
return, so without a bound the attempt waits forever, and the retry envelope
around it, whose classifier lists pull failures as transient, never reaches
attempt 2.

Two rules, because one without the other is still broken: the attempt carries a
`timeout`, and rc=124 is retried while a pull is in progress or failing
(test_swarm_deploy_retry.py). A bound that failed hard would turn one slow pull
into a failed converge, which is worse than the hang it replaced.

Run: uv run pytest tests/unit/test_swarm_deploy_is_bounded.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ansible_tree import command_text as _command_text, walk_tasks

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "infrastructure"
ATTEMPT = ROLE / "tasks" / "_swarm_stack_deploy_attempt.yml"

# The duration is a Jinja expression, and `{{ x }}` contains spaces, so a bare
# \S+ would read the wrapper as absent.
_BOUNDED = re.compile(r"\btimeout\s+(?:\{\{[^}]*\}\}|\S+)\s+docker\b")


def _tasks(path: Path) -> list[dict]:
    """Every task in the file, block/rescue/always included: the classifier
    lives inside the failure-handling block."""
    return walk_tasks(yaml.safe_load(path.read_text()))


def _deploy_tasks() -> list[dict]:
    return [t for t in _tasks(ATTEMPT)
            if "stack deploy" in _command_text(t)]


def test_there_is_a_deploy_task_to_check() -> None:
    """The attempt file still holds a `docker stack deploy` for the checks
    below to read."""
    assert ATTEMPT.is_file(), ATTEMPT
    assert _deploy_tasks(), (
        "no `docker stack deploy` task found in the attempt file, so this test "
        "proves nothing"
    )


def test_the_deploy_attempt_is_bounded() -> None:
    offenders = [
        f"{t.get('name', '<unnamed>')!r}: {_command_text(t).strip()!r}"
        for t in _deploy_tasks()
        if not _BOUNDED.search(_command_text(t))
    ]
    assert not offenders, (
        "--detach=false waits for every task to converge, so an unpullable "
        "image blocks this forever and the retry envelope around it never "
        "runs:\n  " + "\n  ".join(offenders)
    )


def test_the_bound_is_declared_once_as_a_default() -> None:
    """The value lives in defaults so an operator on a slow link can raise it
    without editing a task file."""
    defaults = yaml.safe_load(
        (ROLE / "defaults" / "main.yml").read_text()) or {}
    assert isinstance(defaults.get("swarm_stack_deploy_timeout"), int), (
        "swarm_stack_deploy_timeout must be an integer number of seconds in "
        "reconcile/roles/infrastructure/defaults/main.yml, got "
        f"{defaults.get('swarm_stack_deploy_timeout')!r}"
    )
    for task in _deploy_tasks():
        assert "swarm_stack_deploy_timeout" in _command_text(task), (
            f"{task.get('name')!r} hardcodes its own bound instead of using "
            f"swarm_stack_deploy_timeout"
        )
