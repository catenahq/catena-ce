"""Each attempt of the "Probe docker daemon is serving API requests" task is
bounded by `timeout`, so its retries end on a hung daemon
(bootstrap/roles/docker/tasks/main.yml says why).

Run: uv run pytest tests/unit/test_docker_info_probe_timeout.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "bootstrap" / "roles" / "docker" / "tasks" / "main.yml"
)


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for t in _tasks():
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_probe_wraps_docker_info_in_timeout():
    task = _find("Probe docker daemon is serving API requests")
    cmd = task["ansible.builtin.command"]["cmd"]
    assert cmd.startswith("timeout "), (
        "bare `docker info` blocks forever on a hung daemon; it must be "
        "wrapped in `timeout` so each attempt returns and the retries fire"
    )
    assert "docker info" in cmd


def test_probe_still_retries_until_serving():
    task = _find("Probe docker daemon is serving API requests")
    assert task["retries"] == 15
    assert task["until"] == "_docker_info_probe.rc == 0"
