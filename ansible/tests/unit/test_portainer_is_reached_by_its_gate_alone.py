"""No client app container reaches Portainer's login: Portainer is on its own
overlay, joined by its sign-in gate and the panel alone, never catena-network
(CV2). The host's own callers reach it on the loopback publish.

The converge moves a Portainer it finds on any other network onto that
overlay, with the network drift the panel's spec uses.

Run: uv run pytest tests/unit/test_portainer_is_reached_by_its_gate_alone.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"

_spec = importlib.util.spec_from_file_location(
    "catena_admin_service", ANSIBLE / "playbooks" / "filter_plugins" / "catena_admin_service.py")
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)

NETWORK = "{{ portainer_network }}"


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text()) if isinstance(t, dict)]


def test_portainer_has_a_network_of_its_own():
    name = yaml.safe_load(GROUP_VARS.read_text())["portainer_network"]
    assert name and name != "catena-network"


def test_only_portainers_gate_joins_it():
    apps = yaml.safe_load((ROLES / "oauth2_proxy" / "defaults" / "main.yml").read_text())[
        "oauth2_proxy_apps"]
    joined = [a["slug"] for a in apps if NETWORK in a.get("networks", [])]
    assert joined == ["portainer"]
    gate = next(a for a in apps if a["slug"] == "portainer")
    assert gate["upstream_alias"] == "catena-portainer"


def test_the_panel_joins_it_for_portainer_to_read_its_catalog():
    tasks = _tasks(ROLES / "catena-admin" / "tasks" / "deploy.yml")
    spec = next(t for t in tasks if "build the service spec" in t.get("name", ""))
    assert NETWORK in spec["ansible.builtin.set_fact"]["_ca_spec"]["networks"]
    defaults = (ROLES / "portainer" / "defaults" / "main.yml").read_text()
    assert "http://{{ catena_admin_service_name }}:" in defaults


def test_a_portainer_on_catena_network_is_moved():
    tasks = _tasks(ROLES / "portainer" / "tasks" / "main.yml")
    names = [t.get("name", "") for t in tasks]
    reconcile = names.index("portainer: create or reconcile the swarm service")
    move = names.index("portainer: move it onto its own network")
    assert reconcile < move < names.index("portainer: wait for the UI to answer")
    task = tasks[move]
    assert "catena_admin_network_drift({portainer_network:" in task["vars"]["_pt_network_drift"]
    assert task["when"] == "_pt_network_drift | length > 0"

    on_shared = [{"Spec": {"TaskTemplate": {"Networks": [{"Target": "shared-id"}]}}}]
    assert svc.catena_admin_network_drift(on_shared, {"catena-portainer-net": "own-id"}) == [
        "--network-rm", "shared-id", "--network-add", "catena-portainer-net"]
    on_own = [{"Spec": {"TaskTemplate": {"Networks": [{"Target": "own-id"}]}}}]
    assert svc.catena_admin_network_drift(on_own, {"catena-portainer-net": "own-id"}) == []
