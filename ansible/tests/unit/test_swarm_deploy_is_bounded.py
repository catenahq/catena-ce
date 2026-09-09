"""A swarm stack deploy is bounded, and hitting the bound retries.

THE DEFECT. `docker stack deploy --detach=false` blocks until every task
converges. A task that CANNOT converge -- an image the daemon is unable to
pull -- never lets it return, so the attempt waits forever.

That disarms the recovery written for exactly this failure. The attempt sits
inside a five-iteration retry envelope whose classifier already lists
`i/o timeout` and `temporary failure in name resolution` as transient, and none
of it can run: the loop cannot reach attempt 2 while attempt 1 is still waiting
on the pull that is failing. The only bound left was the bench runner's, and it
kills the whole converge:

    TASK [oauth2_proxy : Swarm: deploy attempt 1 (oauth2-proxy)]
    [runner] TIMEOUT after 4800s

Run 2026-08-26T05-43-28-338d, ce_install_suite stage-4g, on a DNS flake inside
the VM:

    failed to resolve reference "quay.io/oauth2-proxy/oauth2-proxy:v7.15.2-alpine":
    dial tcp: lookup quay.io on 127.0.0.53:53: i/o timeout

The resolver answered again minutes later, so a second attempt would have
succeeded. ce_install_suite is the DAG root, so the failure cost the whole run.

Two rules, because one without the other is still broken: the attempt carries a
`timeout`, and rc=124 counts as transient. A bound that fails hard would turn
one slow pull into a failed converge, which is worse than the hang it replaced.

Run: uv run pytest tests/unit/test_swarm_deploy_is_bounded.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "infrastructure"
ATTEMPT = ROLE / "tasks" / "_swarm_stack_deploy_attempt.yml"

# The duration is a Jinja expression, and `{{ x }}` contains spaces, so a bare
# \S+ would read the wrapper as absent.
_BOUNDED = re.compile(r"\btimeout\s+(?:\{\{[^}]*\}\}|\S+)\s+docker\b")


def _tasks(path: Path) -> list[dict]:
    """Every task in the file, descending into block/rescue/always -- the
    classifier lives inside the failure-handling block."""
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

    _walk(yaml.safe_load(path.read_text()))
    return out


def _command_text(task: dict) -> str:
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


def _deploy_tasks() -> list[dict]:
    return [t for t in _tasks(ATTEMPT)
            if "stack deploy" in _command_text(t)]


def test_there_is_a_deploy_task_to_check() -> None:
    """Guard the guard: a rename would make this vacuous."""
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


def test_hitting_the_bound_is_treated_as_transient() -> None:
    """A bound that failed hard would turn one slow pull into a failed
    converge. rc=124 is transient by construction, not by pattern: the deploy
    only blocks while a task is still trying, and a killed progress stream
    carries no text to match on."""
    classifier = next(
        t for t in _tasks(ATTEMPT)
        if "_swarm_deploy_transient" in str(t.get("ansible.builtin.set_fact", ""))
    )
    expr = str(t_expr := classifier["ansible.builtin.set_fact"]["_swarm_deploy_transient"])
    assert "124" in expr, (
        "the transient classifier does not recognise rc=124, so a bounded "
        f"attempt fails the play instead of retrying: {t_expr!r}"
    )
