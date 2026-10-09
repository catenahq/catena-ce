"""Each infrastructure endpoint Gatus probes is the service its name says, and
a probe through the public edge exists only once the edge serves the domain.

The spec is rendered the way the converge renders it, from the roles'
defaults, with StrictUndefined, and read back as JSON: catena-gatus-sync turns
each entry into one endpoint of 40-infra.yaml.

Run: uv run pytest tests/unit/test_gatus_infra_spec.py
"""
from __future__ import annotations

import json
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
SPEC = ROLES / "infrastructure" / "templates" / "gatus-infra-spec.json.j2"
ZONE = "example.test"


def _vars(*parts: str) -> dict:
    return yaml.safe_load(ROLES.joinpath(*parts).read_text())


def _spec(edge_up: bool = True, zone: str = ZONE) -> list[dict]:
    """The spec as a converge renders it: `edge_up` is the role's
    _infra_edge_up (a Cloudflare token and the tunnel engine), `zone` the
    domain, empty on a host that has none yet."""
    infra = _vars("infrastructure", "defaults", "main.yml")
    keycloak = _vars("keycloak", "defaults", "main.yml")
    keycloak_vars = _vars("keycloak", "vars", "main.yml")
    context = {
        "_infra_edge_up": edge_up,
        "catena_public_surface_deferred": not zone,
        "cloudflare_zone": zone,
        "infrastructure_gatus_hostname": f"gatus.{zone}",
        "catena_admin_hostname": f"dash.{zone}",
        "portainer_admin_hostname": f"portainer.{zone}",
        "healthchecks_hostname": f"healthchecks.{zone}",
        "keycloak_server_alias": keycloak_vars["keycloak_server_alias"],
        "keycloak_management_port": keycloak["keycloak_management_port"],
        "keycloak_health_path": keycloak["keycloak_health_path"],
        "beszel_hub_network_alias": infra["beszel_hub_network_alias"],
        "beszel_hub_internal_port": infra["beszel_hub_internal_port"],
    }
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = bool
    return json.loads(env.from_string(SPEC.read_text()).render(**context))


def _entry(label: str) -> dict:
    found = [e for e in _spec() if e["label"] == label]
    assert len(found) == 1, f"{len(found)} entries labelled {label!r}"
    return found[0]


def test_every_alert_slug_is_unique():
    """The slug names the Healthchecks check an alert lands on; two entries
    sharing one would page as one service."""
    slugs = [e["alert_slug"] for e in _spec()]
    assert len(slugs) == len(set(slugs)), slugs


def test_no_two_entries_probe_the_same_url():
    """An entry named for one service that probes another's address reports
    the other one: a dead tunnel reads green while Traefik answers."""
    urls = [e["url"] for e in _spec()]
    assert len(urls) == len(set(urls)), urls


def test_the_beszel_hub_is_probed_on_every_host():
    """Beszel is base plane, so its probe is unconditional. Its public host
    answers oauth2-proxy's 302 whatever the hub's state, so the hub itself is
    asked, on its network alias."""
    hub = _entry("Beszel hub")
    assert hub["url"] == "http://beszel-hub:8090/api/health"
    assert hub["accepted"] == [200]
    assert hub["alert_slug"] == "beszel-hub"


def test_cloudflared_is_probed_through_the_tunnel():
    """An unrouted name under the domain reaches Traefik's 404 only through a
    working tunnel: a down connector gets the edge's 530, and an ingress that
    does not serve the domain gets the tunnel's 418."""
    tunnel = _entry("cloudflared")
    assert tunnel["url"] == f"https://cf-chain-probe.{ZONE}/"
    assert tunnel["accepted"] == [404]


def test_keycloak_is_probed_inside_the_network():
    """Keycloak's readiness, on the management port its own healthcheck
    reads, over catena-network."""
    keycloak = _entry("Keycloak")
    assert keycloak["url"] == "http://keycloak-server:9000/health/ready"
    assert keycloak["accepted"] == [200]


def test_no_name_under_the_domain_is_probed_before_the_edge_serves_it():
    """A name asked before the domain's wildcard exists is answered NXDOMAIN,
    and resolvers keep that answer for the zone's negative TTL (30 minutes on
    Cloudflare): a probe written before the edge serves the domain reads red
    that long after it does. With no domain the names are `<sub>.` and never
    resolve at all."""
    for edge_up, zone in ((False, ZONE), (True, ""), (False, "")):
        public = [e["url"] for e in _spec(edge_up, zone) if e["url"].startswith("https://")]
        assert public == [], (edge_up, zone, public)


def test_the_edge_probes_arrive_with_the_edge():
    """The converge that brings the edge up for a domain writes every probe
    through it, the tunnel's included."""
    public = {e["label"] for e in _spec() if e["url"].startswith("https://")}
    assert public == {f"gatus.{ZONE}", f"dash.{ZONE}", f"portainer.{ZONE}",
                      f"healthchecks.{ZONE}", "cloudflared"}
