"""reconcile/roles/keycloak reads its multi-domain routing from the served-domain
projection, not from a fact computed in this repo.

One Keycloak serves an auth.<zone> sign-on island per attached domain. Which
domains those are used to arrive as an Ansible fact assembled by a Business
overlay that verified the subscription in Python -- a second implementation of
something the Go side already did, living in a repo that carries no Business
logic (CP2). It now comes from the file the tunnel engine writes after it has
actually brought those domains up.

Run: uv run pytest tests/unit/test_keycloak_zone_projection.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parents[3] / "ansible" / "reconcile" / "roles" / "keycloak"
DEPLOY = _ROLE / "tasks" / "deploy.yml"
DEFAULTS = _ROLE / "defaults" / "main.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(DEPLOY.read_text())


def _find(fragment: str) -> dict:
    for task in _tasks():
        if fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {fragment!r}")


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def test_projection_is_slurped_from_the_default_path():
    task = _find("read the served-domain projection")
    assert task["ansible.builtin.slurp"]["src"] == "{{ keycloak_zone_projection_path }}"
    # A missing file is the normal single-domain case, so the read must not fail
    # the converge.
    assert task["failed_when"] is False


def test_secondaries_come_from_the_filter_with_schema_and_staleness():
    task = _find("resolve the secondary auth domains")
    expr = task["ansible.builtin.set_fact"]["_kc_secondary_zones"]
    assert "catena_served_zones" in expr
    assert "keycloak_zone_projection_schema" in expr
    assert "keycloak_zone_projection_max_age_hours" in expr
    # The projection is base64 from slurp and may be absent.
    assert "b64decode" in expr
    assert "default('')" in expr


def test_extra_hosts_are_built_from_the_resolved_secondaries():
    task = _find("deploy as a swarm stack")
    extra = task["vars"]["svc_domain_extra_hosts"]
    assert "_kc_secondary_zones" in extra
    assert "keycloak_subdomain" in extra
    # The retired fact must be gone: it was the multi-domain overlay's output.
    assert "cloudflare_zones" not in extra


def test_no_licence_semantics_in_the_role():
    """CP2: the routing follows the projection, and the projection already
    reflects the subscription because the engine capped it before writing."""
    body = DEPLOY.read_text().lower()
    for gone in ("catena_ee_multidomain_enabled", "catena_license", "catena_licence"):
        assert gone not in body


def test_defaults_pin_the_projection_contract():
    d = _defaults()
    assert d["keycloak_zone_projection_path"] == "/var/lib/catena/cloudflare-zones.json"
    # Must match payload/engines/cloudflared ProjectionSchema in catena-admin.
    assert d["keycloak_zone_projection_schema"] == 1
    assert d["keycloak_zone_projection_max_age_hours"] > 0
