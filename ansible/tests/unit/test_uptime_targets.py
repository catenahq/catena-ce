"""The services of Catena's own that catena-uptime checks, and the file the
converge renders them into.

The targets file is rendered the way the converge renders it, from the roles'
defaults, with StrictUndefined, and read back as JSON. A check through the
public edge exists only once the edge serves the domain.

Run: uv run pytest tests/unit/test_uptime_targets.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
TEMPLATE = ROLES / "infrastructure" / "templates" / "uptime-targets.json.j2"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
ZONE = "example.test"
SLUG = re.compile(r"^[a-z0-9-]{1,100}$")
# The names the services are reached by inside catena-network: the host runs the
# check, so an address that holds one never resolves.
NETWORK_ALIASES = ("keycloak-server", "beszel-hub:", "catena-traefik", "catena-admin:")


def _vars(*parts: str) -> dict:
    return yaml.safe_load(ROLES.joinpath(*parts).read_text())


def _contract_stacks() -> set[str]:
    group_vars = yaml.safe_load(GROUP_VARS.read_text())
    return {app["stack"] for app in group_vars["default_apps"]}


def _file(edge_up: bool = True, zone: str = ZONE) -> dict:
    """The file as a converge renders it: `edge_up` is the role's
    _infra_edge_up (a Cloudflare token and the tunnel engine), `zone` the
    domain, empty on a host that has none yet."""
    infra = _vars("infrastructure", "defaults", "main.yml")
    keycloak = _vars("keycloak", "defaults", "main.yml")
    context = {
        "_infra_edge_up": edge_up,
        "catena_public_surface_deferred": not zone,
        "cloudflare_zone": zone,
        "catena_admin_hostname": f"dash.{zone}",
        "keycloak_hostname": f"auth.{zone}",
        "beszel_hostname": f"beszel.{zone}",
        "keycloak_realm": keycloak["keycloak_realm"],
        "beszel_hub_host_localhost_port": infra["beszel_hub_host_localhost_port"],
        "beszel_hub_stack": "catena-beszel",
    }
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = bool
    return json.loads(env.from_string(TEMPLATE.read_text()).render(**context))


def _targets(edge_up: bool = True, zone: str = ZONE) -> list[dict]:
    return _file(edge_up, zone)["targets"]


def _entry(name: str) -> dict:
    found = [t for t in _targets() if t["name"] == name]
    assert len(found) == 1, f"{len(found)} targets named {name!r}"
    return found[0]


def test_the_file_is_version_1_and_its_path_is_a_role_default():
    assert _file()["version"] == 1
    assert _vars("infrastructure", "defaults", "main.yml")["uptime_targets_path"] == (
        "/etc/catena/uptime-targets.json")


def test_every_target_holds_the_fields_the_check_reads():
    """An unknown field makes catena-uptime read none of the file."""
    for edge_up, zone in ((True, ZONE), (False, ZONE), (True, ""), (False, "")):
        for target in _targets(edge_up, zone):
            assert set(target) <= {"name", "name_fr", "url", "accepted", "alert_slug", "stack"}, target
            assert {"name", "name_fr", "url", "accepted", "alert_slug"} <= set(target), target
            assert 1 <= len(target["name"]) <= 80 and 1 <= len(target["name_fr"]) <= 80
            assert 1 <= len(target["accepted"]) <= 8
            assert all(100 <= code <= 599 for code in target["accepted"])


def test_every_alert_slug_is_valid_and_unique():
    """The slug is the id the alert intake keeps the target under; two entries
    sharing one would page as one service."""
    slugs = [t["alert_slug"] for t in _targets()]
    assert all(SLUG.match(slug) for slug in slugs), slugs
    assert len(slugs) == len(set(slugs)), slugs


def test_the_slugs_are_the_ones_the_hub_holds_open_rows_under():
    """Each address's slug is its host, lower case, with dots as dashes: the
    key a row already open carries over under."""
    by_name = {t["name"]: t["alert_slug"] for t in _targets()}
    assert by_name == {
        "Administration panel": "dash-example-test",
        "Sign-in (Keycloak)": "auth-example-test",
        "Resource monitor (Beszel)": "beszel-example-test",
        "Cloudflare tunnel": "cloudflared",
        "Beszel hub": "beszel-hub",
    }


def test_no_two_targets_check_the_same_url():
    urls = [t["url"] for t in _targets()]
    assert len(urls) == len(set(urls)), urls


def test_every_stack_is_a_default_app_stack():
    """catena-uptime leaves out an entry whose stack has no folder: a stack no
    default app owns would drop the entry for good."""
    stacks = {t["stack"] for t in _targets() if "stack" in t}
    assert stacks <= _contract_stacks(), stacks


def test_the_default_apps_name_their_stack():
    stacked = {t["name"]: t["stack"] for t in _targets() if "stack" in t}
    assert stacked == {"Resource monitor (Beszel)": "catena-beszel", "Beszel hub": "catena-beszel"}
    assert [t["name"] for t in _targets(False) if "stack" in t] == ["Beszel hub"]


def test_no_url_names_an_address_only_the_network_resolves():
    """The check runs on the host, outside catena-network."""
    for edge_up, zone in ((True, ZONE), (False, ZONE), (True, "")):
        for target in _targets(edge_up, zone):
            assert not any(alias in target["url"] for alias in NETWORK_ALIASES), target["url"]


def test_the_beszel_hub_is_checked_on_every_host_on_its_loopback_publish():
    """Beszel's public host answers oauth2-proxy's 302 whatever the hub's
    state, so the hub itself is asked, on the loopback port it is published on."""
    port = _vars("infrastructure", "defaults", "main.yml")["beszel_hub_host_localhost_port"]
    for edge_up, zone in ((True, ZONE), (False, ZONE), (True, ""), (False, "")):
        hubs = [t for t in _targets(edge_up, zone) if t["name"] == "Beszel hub"]
        assert len(hubs) == 1
        assert hubs[0]["url"] == f"http://127.0.0.1:{port}/api/health"
        assert hubs[0]["accepted"] == [200]


def test_cloudflared_is_checked_through_the_tunnel():
    """An unrouted name under the domain reaches Traefik's 404 only through a
    working tunnel: a down connector gets the edge's 530, and an ingress that
    does not serve the domain gets the tunnel's 418."""
    tunnel = _entry("Cloudflare tunnel")
    assert tunnel["url"] == f"https://cf-chain-probe.{ZONE}/"
    assert tunnel["accepted"] == [404]


def test_the_public_addresses_and_what_answers_up():
    assert {t["name"]: (t["url"], t["accepted"]) for t in _targets() if t["url"].startswith("https://")} == {
        "Administration panel": (f"https://dash.{ZONE}", [302]),
        "Sign-in (Keycloak)": (f"https://auth.{ZONE}/realms/vps/.well-known/openid-configuration", [200]),
        "Resource monitor (Beszel)": (f"https://beszel.{ZONE}", [302]),
        "Cloudflare tunnel": (f"https://cf-chain-probe.{ZONE}/", [404]),
    }


def test_no_name_under_the_domain_is_checked_before_the_edge_serves_it():
    """A name asked before the domain's wildcard exists is answered NXDOMAIN,
    and resolvers keep that answer for the zone's negative TTL (30 minutes on
    Cloudflare): a check written before the edge serves the domain reads red
    that long after it does. With no domain the names are `<sub>.` and never
    resolve at all."""
    for edge_up, zone in ((False, ZONE), (True, ""), (False, "")):
        public = [t["url"] for t in _targets(edge_up, zone) if t["url"].startswith("https://")]
        assert public == [], (edge_up, zone, public)


def test_the_edge_checks_arrive_with_the_edge():
    assert len([t for t in _targets() if t["url"].startswith("https://")]) == 4
