"""The portainer role gives every client stack an application folder and no
longer points Portainer at a catalog of this host's.

`catena-apps adopt` writes a folder for each client stack Portainer holds and
redeploys it from there. The converge holds /run/catena.lock for the whole
play, so the verb runs with -held; Portainer's key rides the environment, never
argv.

Run: uv run pytest tests/unit/test_portainer_adopts_client_stacks.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "portainer"


def _tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())


def _named(fragment: str) -> dict:
    for task in _tasks():
        if fragment in task.get("name", ""):
            return task
    raise AssertionError(f"no task named ...{fragment}...")


def test_the_adoption_runs_inside_the_converges_hold_and_carries_no_secret():
    """Portainer's key is read from the on-box store, where the mint wrote it:
    the command line and the environment carry the address alone."""
    task = _named("adopt the client stacks as application folders")
    cmd = task["ansible.builtin.command"]
    assert cmd["argv"] == ["/usr/local/bin/catena-apps", "adopt", "-held"]
    assert task["environment"] == {"PORTAINER_API_BASE": "{{ portainer_api_base_onbox }}"}
    assert "portainer_api_key" not in " ".join(cmd["argv"])
    assert task["no_log"] is True


def test_the_adoption_waits_for_an_installed_binary_and_a_key():
    task = _named("adopt the client stacks as application folders")
    assert "_pt_apps_bin.stat.exists" in task["when"]
    assert "portainer_api_key | length > 10" in task["when"]


def test_a_failed_adoption_fails_the_converge_and_a_refused_stack_is_named():
    assert "_pt_adopt.rc | default(0) != 0" == _named("the adoption failed")["when"]
    said = _named("say which stacks stay on Portainer")
    assert "refused" in said["ansible.builtin.debug"]["msg"]


def test_the_role_sets_no_app_templates_url():
    text = (_ROLE / "tasks" / "main.yml").read_text() + (_ROLE / "defaults" / "main.yml").read_text()
    for gone in ("TemplatesURL", "portainer_templates_url", "/settings"):
        assert gone not in text, gone
