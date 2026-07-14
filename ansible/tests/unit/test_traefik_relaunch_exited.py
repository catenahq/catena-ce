"""Lock the traefik relaunch-if-not-running self-heal.

catena-traefik is a PLAIN container that attaches to catena-network by
network ID. When the overlay is torn down + recreated (a restore rebuilds
swarm+overlay; an interrupted converge or daemon flap can too), the new
overlay has a fresh ID and the container's baked-in reference is stale:
`docker start` fails ("network <old-id> not found") and it sits Exited.
Swarm services rejoin by NAME and self-heal; this plain container cannot,
so ingress stays dead. The relaunch decision must therefore fire on a
not-running container -- the image/ports/acme conditions never catch an
exited one.

Run: uv run pytest tests/unit/test_traefik_relaunch_exited.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "traefik" / "tasks" / "main.yml"
)


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for t in _tasks():
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_running_state_is_read():
    task = _find("read running state")
    fmt = task["ansible.builtin.command"]
    assert ".State.Running" in fmt
    # gated on the container existing
    assert task["when"] == "_traefik_inspect.rc == 0"


def test_relaunch_fires_on_not_running():
    task = _find("decide whether catena-traefik needs (re)launch")
    expr = task["ansible.builtin.set_fact"]["_traefik_needs_relaunch"]
    assert "_traefik_running_state.stdout" in expr
    assert "!= 'true'" in expr
    # only when the container actually exists (rc==0), else the rc!=0 arm owns it
    assert "_traefik_inspect.rc == 0" in expr


def test_relaunch_still_covers_gone_and_drift():
    # the new condition must be additive, not a replacement.
    task = _find("decide whether catena-traefik needs (re)launch")
    expr = task["ansible.builtin.set_fact"]["_traefik_needs_relaunch"]
    assert "_traefik_inspect.rc != 0" in expr
    assert "_traefik_running_image.stdout" in expr
    assert "_traefik_ports.stdout" in expr
