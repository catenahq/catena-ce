"""Unit tests for helpers.bootstrap_output.apply_to_inventory.

bootstrap.yml emits the real tailnet ansible_host into .bootstrap-output.yml;
apply_to_inventory folds it into hosts.yml so a separate site/validate
invocation reaches the host instead of the 0.0.0.0 placeholder.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import bootstrap_output  # noqa: E402


def _write(p: Path, data: dict) -> None:
    p.write_text(yaml.safe_dump(data, default_flow_style=False))


def test_apply_folds_tailnet_ip_into_hosts_yml(tmp_path):
    inv = tmp_path / "inventory" / "test"
    inv.mkdir(parents=True)
    _write(inv / ".bootstrap-output.yml", {
        "inventory_dir": str(inv),
        "hosts": {"testvm-a": {"ansible_host": "100.122.177.79"}},
        "vault": {},
    })
    _write(inv / "hosts.yml", {
        "all": {"children": {"vps": {"hosts": {
            "testvm-a": {"ansible_host": "0.0.0.0", "ansible_user": "ops"},
        }}}}
    })

    applied = bootstrap_output.apply_to_inventory(inv)

    assert applied == ["testvm-a.ansible_host=100.122.177.79"]
    hosts = yaml.safe_load((inv / "hosts.yml").read_text())
    vps = hosts["all"]["children"]["vps"]["hosts"]["testvm-a"]
    assert vps["ansible_host"] == "100.122.177.79"
    # Other host vars are preserved.
    assert vps["ansible_user"] == "ops"


def test_apply_is_noop_without_output_file(tmp_path):
    inv = tmp_path / "inventory" / "test"
    inv.mkdir(parents=True)
    assert bootstrap_output.apply_to_inventory(inv) == []
