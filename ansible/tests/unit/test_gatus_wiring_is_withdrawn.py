"""Core installs and wires no Gatus, and removes only the files it wrote for it.

A copy already running is left alone: no task names its stack or its overlay, so
none can stop, remove or convert it, and it stays an ordinary catalog app the
admin updates or removes from the panel. The files the converge itself wrote
for the monitor (its sync env, its infra spec, its ports fragment, its summary)
go with `state: absent` tasks, and validate.yml asserts each is gone.

Run: uv run pytest tests/unit/test_gatus_wiring_is_withdrawn.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"
TREES = [ANSIBLE / name for name in ("playbooks", "reconcile", "bootstrap", "helpers", "scripts")]
SUFFIXES = {".yml", ".yaml", ".j2", ".py", ".sh", ".json"}
# What the converge wired the app by: its stack and overlay, the sync that fed
# it, and the variables that held its address, ports, volumes and secret.
WIRING = ("catena-gatus", "gatus_stack", "gatus_network", "gatus_hostname", "gatus_subdomain",
          "gatus_host_", "gatus_data_volume", "gatus_sqlite", "gatus_internal_port",
          "gatus_infra_spec", "gatus_sync", "gatus-sync", "gatus-infra", "gatus_summary",
          "_gatus_sync_present")
# The one place that names the files to remove, and the secret the store drops.
NAMES_THE_REMOVAL = {
    INFRA / "defaults" / "main.yml",
    ANSIBLE / "helpers" / "onbox_config.py",
}

sys.path.insert(0, str(ANSIBLE))


def _files():
    for tree in TREES:
        for path in sorted(tree.rglob("*")):
            if path.is_file() and path.suffix in SUFFIXES:
                yield path


def _defaults() -> dict:
    return yaml.safe_load((INFRA / "defaults" / "main.yml").read_text())


def test_gatus_is_not_a_default_app():
    gv = yaml.safe_load((ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml").read_text())
    assert [a["app"] for a in gv["default_apps"]] == ["beszel"]


def test_no_file_names_the_gatus_stack_overlay_or_sync():
    """The only names left are the files the removal lists; the same check for
    a dropped default app is in test_a_dropped_default_app_is_left_alone.py."""
    checked = 0
    for path in _files():
        if path in NAMES_THE_REMOVAL or path == Path(__file__):
            continue
        text = path.read_text(encoding="utf-8")
        checked += 1
        for name in WIRING:
            assert name not in text, f"{path.relative_to(ANSIBLE)} names {name}"
    assert checked > 100


def test_the_removal_names_only_the_files_the_converge_wrote():
    removed = _defaults()["infrastructure_withdrawn_files"]
    assert removed == [
        "/etc/catena/gatus-sync.env",
        "/etc/catena/gatus-infra-spec.json",
        "{{ public_ports_fragment_dir }}/gatus.json",
        "{{ backup_stats_dir | default('/var/lib/catena') }}/gatus-summary.json",
    ]


def test_the_role_removes_each_file_and_nothing_else():
    tasks = yaml.safe_load((INFRA / "tasks" / "withdrawn.yml").read_text())
    [task] = tasks
    assert task["ansible.builtin.file"] == {"path": "{{ item }}", "state": "absent"}
    assert task["loop"] == "{{ infrastructure_withdrawn_files }}"
    main = yaml.safe_load((INFRA / "tasks" / "main.yml").read_text())
    include = next(t for t in main
                   if (t.get("ansible.builtin.include_tasks") or {}).get("file") == "withdrawn.yml")
    assert include["tags"] == ["withdrawn"]


def test_validate_asserts_each_file_is_gone():
    tasks = yaml.safe_load((INFRA / "tasks" / "validate.yml").read_text())
    stat = next(t for t in tasks if "withdrawn monitor wiring" in t["name"] and "ansible.builtin.stat" in t)
    assert stat["loop"] == "{{ infrastructure_withdrawn_files }}"
    check = next(t for t in tasks if "ansible.builtin.assert" in t
                 and "none of the withdrawn" in t["name"])
    assert check["ansible.builtin.assert"]["that"] == ["not item.stat.exists"]


def test_the_ports_fragment_is_reconciled_by_the_converge_tail():
    """The removed fragment's rule drops when the registry is reconciled, which
    the tail of every converge does."""
    tail = (ANSIBLE / "playbooks" / "tasks" / "converge_tail.yml").read_text()
    assert "name: catena-public-ports.service" in tail


def test_the_gate_of_the_removed_monitor_is_not_an_entry():
    gates = yaml.safe_load(
        (ANSIBLE / "reconcile" / "roles" / "oauth2_proxy" / "defaults" / "main.yml").read_text())
    assert [a["slug"] for a in gates["oauth2_proxy_apps"]] == ["catena-admin", "beszel"]


def test_its_cookie_secret_is_dropped_from_the_store_and_never_minted():
    from helpers import onbox_config

    assert "gatus_gate_cookie_secret" in onbox_config.RETIRED_SECRETS
    assert "gatus_gate_cookie_secret" not in onbox_config.INTERNAL_SECRETS


def test_the_version_report_wiring_outlives_the_sync():
    """The refresh at the end of the converge keys on whether the script is
    here, a fact its own task pins."""
    tail = (ANSIBLE / "playbooks" / "tasks" / "converge_tail.yml").read_text()
    assert "_version_check_present" in tail
    pins = yaml.safe_load((INFRA / "tasks" / "version_check.yml").read_text())
    assert any("_version_check_present" in (t.get("ansible.builtin.set_fact") or {}) for t in pins)
    assert not re.search(r"systemctl (enable|start)", (INFRA / "tasks" / "version_check.yml").read_text())
