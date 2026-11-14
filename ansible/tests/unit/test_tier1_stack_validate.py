"""reconcile/roles/tier1_stack/tasks/validate.yml checks the stack dir and file
modes (the file names every swarm secret the control plane mounts and stays
root's) and that `docker stack config` loads the file with every required
service.

Run: uv run pytest tests/unit/test_tier1_stack_validate.py
"""
from __future__ import annotations

import yaml

from ansible_jinja import holds
from ansible_tree import role_dir

TASKS = yaml.safe_load((role_dir("tier1_stack") / "tasks" / "validate.yml").read_text())
DEFAULTS = yaml.safe_load((role_dir("tier1_stack") / "defaults" / "main.yml").read_text())


def _task(fragment: str) -> dict:
    for t in TASKS:
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def test_the_dir_is_0750_and_the_file_0640_both_root():
    paths = _task("the stack dir and the rendered")["vars"]["_assert_paths"]
    assert [(p["path"], p["type"], p["owner"], p["group"], p["mode"]) for p in paths] == [
        ("{{ tier1_stack_dir }}", "directory", "root", "root", "0750"),
        ("{{ tier1_stack_path }}", "file", "root", "root", "0640"),
    ]


def test_the_compose_loader_reads_the_rendered_file():
    argv = _task("load the rendered file")["ansible.builtin.command"]["argv"]
    assert argv == ["docker", "stack", "config", "--compose-file", "{{ tier1_stack_path }}"]


def _loads(rc: int, services: dict) -> bool:
    out = yaml.safe_dump({"services": services}) if rc == 0 else ""
    return holds(_task("holds every control-plane service"), "ansible.builtin.assert",
                 _t1_val_config={"rc": rc, "stdout": out},
                 tier1_stack_required=DEFAULTS["tier1_stack_required"])


def test_every_required_service_must_be_in_the_loaded_file():
    assert _loads(0, {"catena-traefik": {}, "catena-postgres": {}})
    assert not _loads(0, {"catena-traefik": {}})
    assert not _loads(0, {})


def test_a_file_the_loader_rejects_fails():
    assert not _loads(1, {})
