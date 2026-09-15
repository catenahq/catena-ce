"""Every systemctl call that brings docker.service up is time-bounded.

WHY THIS IS NOT COVERED BY THE API PROBE. The docker role already bounds its
first API call: `timeout 10 docker info` with retries, so a daemon whose socket
accepts but never answers makes the converge abort in about three minutes
instead of blocking on one task. That guard is correct and it never fired.

docker.service is Type=notify with TimeoutStartSec=0, so systemd waits for
dockerd's READY=1 with no bound of its own. `systemctl restart docker` and
`systemctl enable --now docker` therefore inherit that unboundedness, and both
run BEFORE the probe -- the restart via the role's `meta: flush_handlers`,
which fires whenever daemon.json changed. Against an unresponsive daemon the
converge hangs at the systemctl call and the probe's bound is unreachable.

Observed on bench run 2026-08-25T18-01-29-b726: fi_c1 froze dockerd, confirmed
state T, and the converge still returned rc=0 with ok=553 failed=0. The restart
handler SIGKILLed the frozen daemon and started a healthy one before the probe
asked it anything, so the run repaired the fault it existed to measure.

The rule this pins: a systemctl call against docker.service in this role is
wrapped in `timeout`. `timeout` kills the systemctl CLIENT and leaves the
systemd job running, which is the intent -- bound the converge, not the
restart.

Run: uv run pytest tests/unit/test_docker_daemon_control_is_bounded.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "bootstrap" / "roles" / "docker"

# A systemctl verb that can wait on dockerd's readiness notification. `stop`,
# `is-active` and friends return on their own and are not in scope.
_BLOCKING = re.compile(r"systemctl\s+(?:[-\w]+\s+)*(?:restart|start|reload)\b|"
                       r"systemctl\s+(?:[-\w]+\s+)*enable\b[^\n]*--now")
# The duration is a Jinja expression in practice, and `{{ x }}` has spaces in
# it, so a bare \S+ would read the wrapper as absent.
_BOUNDED = re.compile(r"\btimeout\s+(?:\{\{[^}]*\}\}|\S+)\s+systemctl\b")


def _files() -> list[Path]:
    return sorted(ROLE.rglob("*.yml"))


def _tasks(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict)]


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


def test_there_are_role_files_to_check() -> None:
    """Guard the guard: a move or rename would make this vacuous."""
    assert (ROLE / "handlers" / "main.yml").is_file(), ROLE
    assert (ROLE / "tasks" / "main.yml").is_file(), ROLE


def test_every_blocking_systemctl_call_on_docker_is_wrapped_in_timeout() -> None:
    offenders: list[str] = []
    checked = 0
    for path in _files():
        for task in _tasks(path):
            text = _command_text(task)
            if "docker" not in text or not _BLOCKING.search(text):
                continue
            checked += 1
            if not _BOUNDED.search(text):
                name = task.get("name", "<unnamed>")
                offenders.append(
                    f"{path.relative_to(ANSIBLE)}: {name!r} runs "
                    f"{text.strip()!r} unbounded"
                )
    assert checked, (
        "no blocking systemctl call against docker was found at all, so this "
        "test proves nothing. The role moved or the call shape changed."
    )
    assert not offenders, (
        "docker.service is Type=notify with TimeoutStartSec=0, so these block "
        "forever against a daemon that never signals ready:\n  "
        + "\n  ".join(offenders)
    )


def test_the_bound_is_declared_once_as_a_default() -> None:
    """The value lives in defaults, so the two call sites cannot drift and an
    operator on a slow host can raise it without editing tasks."""
    defaults = yaml.safe_load(
        (ROLE / "defaults" / "main.yml").read_text()) or {}
    assert isinstance(defaults.get("docker_daemon_control_timeout"), int), (
        "docker_daemon_control_timeout must be an integer number of seconds "
        f"in bootstrap/roles/docker/defaults/main.yml, got "
        f"{defaults.get('docker_daemon_control_timeout')!r}"
    )
    for path in (ROLE / "handlers" / "main.yml", ROLE / "tasks" / "main.yml"):
        for task in _tasks(path):
            text = _command_text(task)
            if "systemctl" in text and "docker" in text and "timeout " in text:
                assert "docker_daemon_control_timeout" in text, (
                    f"{path.relative_to(ANSIBLE)}: {task.get('name')!r} "
                    f"hardcodes its own bound instead of using "
                    f"docker_daemon_control_timeout"
                )
