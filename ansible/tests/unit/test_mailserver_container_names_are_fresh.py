"""A swarm task name is true for an instant, so it is resolved where it is used.

THE DEFECT, three times over. The mailserver chain resolved the dms container
in one task and used that name in later ones. docker-mailserver restarts its
way out of the very state the chain repairs -- it polls 120s for a mailbox and
shuts down without one -- and every restart mints a new swarm task id. So a
name captured a few tasks earlier is routinely dead by the time it is used:

    Error response from daemon: No such container:
        catena-mailserver_dms.1.rg3u7p8hjh2vom6052uoz4322

That failed the whole converge on bench run 2026-08-25T18-01-29-b726, seconds
after the same converge had successfully created the mailbox that lets dms
boot. The work was done and then thrown away by the next task.

The rule this pins: any `docker exec` / `docker cp` against a mailserver
container resolves the name INSIDE its own command. The cert deploy hook
already did it that way, which is why the cert path kept working while the
OIDC path did not.

`| length` gates on a previously-resolved name are fine and stay: they ask
"was the stack deployed", which does not go stale in the same way -- a stack
that existed at the start of the play still exists.

Run: uv run pytest tests/unit/test_mailserver_container_names_are_fresh.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TASKS = ANSIBLE / "roles" / "infrastructure" / "tasks"

# Names the chain resolves once and must not interpolate into a docker command.
CAPTURED = ("mailserver_dms_ct", "_roundcube_ct.stdout", "_ms_cert_dms",
            "_ms_acct_dms", "_dms_ct.stdout")

MAILSERVER_FILES = sorted(TASKS.glob("mailserver_*.yml")) + [
    TASKS / "_mailserver_dms_locate.yml"
]


def _tasks(path: Path) -> list[dict]:
    return [t for t in (yaml.safe_load(path.read_text()) or [])
            if isinstance(t, dict)]


def _command_text(task: dict) -> str:
    """Everything this task actually executes, across the module shapes the
    chain uses (command/argv, command/cmd, shell)."""
    out: list[str] = []
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
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


def test_there_are_mailserver_files_to_check() -> None:
    """Guard the guard: a rename would make this vacuous."""
    assert len(MAILSERVER_FILES) >= 4, MAILSERVER_FILES


def test_no_docker_command_uses_a_previously_resolved_container_name() -> None:
    offenders: list[str] = []
    for path in MAILSERVER_FILES:
        if not path.is_file():
            continue
        for task in _tasks(path):
            text = _command_text(task)
            if "docker exec" not in text and "docker cp" not in text:
                if not re.search(r"\bdocker\b.*\b(exec|cp)\b", text):
                    continue
            for var in CAPTURED:
                if "{{ " + var in text or "{{" + var in text:
                    offenders.append(
                        f"{path.name}: {task.get('name')!r} interpolates "
                        f"{var} into a docker command")
    assert not offenders, (
        "resolve the container inside the command instead:\n  "
        + "\n  ".join(offenders))


def test_the_locator_itself_still_resolves_from_docker_ps() -> None:
    """The shared locate step is where a `docker ps` belongs."""
    text = (TASKS / "_mailserver_dms_locate.yml").read_text()
    assert "label=vps.component=dms" in text
    assert "docker" in text and "service" in text and "ls" in text, (
        "deployment is decided by the swarm SERVICE, which does not blink "
        "between container restarts"
    )
