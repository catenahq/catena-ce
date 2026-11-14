"""reconcile/roles/catena-admin/tasks/validate.yml checks the lane files (the
update lane's settings stay root's), the UI-port fragment (the native-login
port answers this host only), the catalog files and the root-only dispatch
overlay dir, and that the panel service reaches 1/1.

Run: uv run pytest tests/unit/test_catena_admin_validate.py
"""
from __future__ import annotations

import json

import yaml

from ansible_jinja import holds
from ansible_tree import role_dir, walk_tasks

TASKS = walk_tasks(yaml.safe_load((role_dir("catena-admin") / "tasks" / "validate.yml").read_text()))
ASSERT = "ansible.builtin.assert"


def _task(fragment: str) -> dict:
    for t in TASKS:
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _modes(fragment: str) -> dict:
    return {p["path"]: (p["type"], p["owner"], p["group"], p["mode"])
            for p in _task(fragment)["vars"]["_assert_paths"]}


def test_the_update_lane_settings_are_root_0600_and_the_rest_0644():
    modes = _modes("the lanes' configuration files")
    file0644 = ("file", "root", "root", "0644")
    assert modes["{{ catena_stack_update_env_path }}"] == ("file", "root", "root", "0600")
    assert modes["{{ catena_daily_env_path }}"] == file0644
    assert modes["{{ catena_managed_services_path }}"] == file0644


def test_the_catalog_files_are_0644_and_the_overlay_dir_root_0755():
    modes = _modes("the catalog files and the dispatch overlay dir")
    overlay = modes.pop("{{ catena_admin_actions_overlay_dir }}")
    assert overlay == ("directory", "root", "root", "0755")
    assert set(modes) == {
        "{{ catena_admin_actions_yml_path }}",
        "{{ catena_admin_actions_extra_yml_path }}",
        "{{ catena_admin_extra_tiles_path }}",
        "{{ catena_admin_allowed_actions_path }}",
        "{{ catena_admin_dispatch_actions_path }}",
    }
    assert set(modes.values()) == {("file", "root", "root", "0644")}


def test_both_json_files_are_parsed_and_a_bad_one_fails_the_validate():
    parse = _task("parse the managed-services list")["ansible.builtin.set_fact"]
    assert "from_json" in parse["_ca_val_managed"] and "from_json" in parse["_ca_val_ports"]
    block = next(t for t in TASKS if t.get("rescue"))
    assert "ansible.builtin.fail" in block["rescue"][0]


def _ports(entries: object, port: int = 9010) -> bool:
    return holds(_task("native-login port answers this host only"), ASSERT,
                 _ca_val_ports=json.loads(json.dumps(entries)),
                 catena_admin_ui_port=str(port), public_ports_fragment_dir="/f")


def _entry(scope: str, port: int = 9010) -> dict:
    return {"proto": "tcp", "port": port, "scope": scope}


def test_the_ui_port_must_be_declared_loopback_only():
    assert _ports([_entry("loopback")])
    assert not _ports([_entry("public")])
    assert not _ports([_entry("loopback"), _entry("public")])


def test_a_fragment_without_the_ui_port_fails():
    assert not _ports([])
    assert not _ports([_entry("loopback", port=9999)])
    assert not _ports({"port": 9010, "scope": "loopback"})


def _replicas(lines: list[str], rc: int = 0) -> bool:
    return holds(_task("the panel service is 1/1"), ASSERT,
                 _ca_val_service={"rc": rc, "stdout_lines": lines},
                 catena_admin_service_name="catena-admin")


def test_the_service_must_show_one_of_one():
    assert _replicas(["catena-admin|1/1"])
    assert not _replicas(["catena-admin|0/1"])
    assert not _replicas([])


def test_the_replica_check_is_polled_with_a_bound_and_follows_the_deploy_switch():
    poll = _task("swarm service replicas")
    assert (poll["retries"], poll["delay"]) == (24, 5)
    assert poll["when"] == "catena_admin_deploy_from_converge | bool"
    assert _task("the panel service is 1/1")["when"] == "catena_admin_deploy_from_converge | bool"
