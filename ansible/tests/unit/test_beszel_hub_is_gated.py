"""Beszel's hub is public only through its own admin-only oauth2-proxy.

The hub deploys as a swarm stack with no route of its own, on a network
Traefik is not on. dashboard-sync gates only Portainer stacks, so its one
public route is the one reconcile/roles/oauth2_proxy writes from
oauth2_proxy_apps. The hub's own login is behind that gate: its password is
the shared admin password, and its Keycloak sign-in creates a hub account for
any realm user (USER_CREATION in beszel-hub.compose.yml.j2).

Run: uv run pytest tests/unit/test_beszel_hub_is_gated.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
INFRA = ROLES / "infrastructure"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def test_the_hub_has_an_admin_only_oauth2_proxy():
    apps = _yaml(ROLES / "oauth2_proxy" / "defaults" / "main.yml")["oauth2_proxy_apps"]
    infra = _yaml(INFRA / "defaults" / "main.yml")
    [hub] = [a for a in apps if a["host_var"] == "beszel_hostname"]
    assert hub["allowed_groups"] == ["admin"]
    assert hub["upstream_alias"] == infra["beszel_hub_network_alias"]
    assert hub["upstream_port"] == infra["beszel_hub_internal_port"]
    assert hub["slug"] != hub["upstream_alias"], (
        "the proxy's alias would collide with the hub's on their shared network")


def test_the_hostname_is_declared_where_every_play_sees_it():
    """playbooks/validate.yml probes the gate from plays that load no role
    defaults, and a hostname they cannot see drops out of the probe set
    silently."""
    group_vars = _yaml(ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml")
    assert "beszel_hostname" in group_vars
    assert "beszel_hostname" not in _yaml(INFRA / "defaults" / "main.yml")


def test_the_gate_is_probed():
    tasks = _yaml(INFRA / "tasks" / "verify_gated_services.yml")
    probe = next(t for t in tasks if "probe public URLs" in t.get("name", ""))
    assert {"name": "beszel", "url": "https://{{ beszel_hostname }}/"} in probe["loop"]

    validate = (ANSIBLE / "playbooks" / "validate.yml").read_text()
    build_set = validate.split('- name: "Build gated-host probe set"', 1)[1]
    build_set = build_set.split("- name:", 1)[0]
    assert "beszel_hostname" in build_set, (
        "validate does not ask the hub for its /oauth2/start redirect")

