"""A client app container cannot claim a signed-in identity at the panel or at
Healthchecks, nor sign in to Beszel's hub.

Both take the signed-in user from headers their oauth2-proxy gate sets, and
every client app joins catena-network, where it can set any header. The panel
therefore takes those headers only beside the gate secret its gate sends as a
Basic password (catena-admin shell/auth Gate). Healthchecks cannot check a
secret, so it is on a network no client app joins. Beszel's hub creates an
account for any realm user who drives its sign-in, so it is on another such
network, with the alert shim it calls.

The compose templates are rendered the way the converge renders them and read
back as YAML.

Run: uv run pytest tests/unit/test_gated_identity_is_not_forgeable.py
"""
from __future__ import annotations

import json
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
INFRA = ROLES / "infrastructure" / "templates"

HC_NETWORK = "catena-healthchecks"
BESZEL_NETWORK = "catena-beszel"
GATE_SECRET = "gate+secret/value=="


def _flatten(value):
    out = []
    for item in value:
        out.extend(_flatten(item) if isinstance(item, list) else [item])
    return out


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["flatten"] = _flatten
    env.filters["bool"] = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes", "on")
    env.filters["to_json"] = json.dumps
    return env


def _gated_apps() -> list[dict]:
    """oauth2_proxy_apps from the role defaults, its Jinja strings rendered."""
    defaults = yaml.safe_load((ROLES / "oauth2_proxy" / "defaults" / "main.yml").read_text())
    env = _env()
    apps = []
    for app in defaults["oauth2_proxy_apps"]:
        app = dict(app)
        if "networks" in app:
            app["networks"] = [
                env.from_string(n).render(healthchecks_network=HC_NETWORK,
                                          beszel_network=BESZEL_NETWORK)
                for n in app["networks"]
            ]
        apps.append(app)
    return apps


def _gates() -> dict:
    variables = {"catena_admin_gate_secret": GATE_SECRET}
    rendered = _env().from_string(
        (ROLES / "oauth2_proxy" / "templates" / "oauth2-proxy.compose.yml.j2").read_text()
    ).render(
        ansible_managed="",
        oauth2_proxy_apps=_gated_apps(),
        oauth2_proxy_image="quay.io/oauth2-proxy/oauth2-proxy:test",
        oauth2_proxy_client_secret="cs",
        oauth2_proxy_cookie_secret="ck",
        oauth2_proxy_internal_port=4180,
        oauth2_proxy_oidc_issuer="https://auth.acme.test/realms/vps",
        oauth2_proxy_login_url="https://auth.acme.test/login",
        oauth2_proxy_redeem_url="http://keycloak-server:8080/token",
        oauth2_proxy_jwks_url="http://keycloak-server:8080/certs",
        oauth2_proxy_client_id="oauth2-proxy",
        oauth2_proxy_cookie_name="_oauth2_proxy",
        cloudflare_zone="acme.test",
        lookup=lambda kind, name: variables[name],
    )
    return yaml.safe_load(rendered)


def _compose(name: str, **extra) -> dict:
    variables = dict(
        ansible_managed="",
        healthchecks_network=HC_NETWORK,
        beszel_network=BESZEL_NETWORK,
        healthchecks_network_alias="healthchecks",
        healthchecks_internal_port=8000,
        healthchecks_ping_key="pk",
        beszel_hc_shim_image="python:test",
        beszel_hc_shim_internal_port=8099,
        beszel_hc_shim_network_alias="beszel-hc-shim",
        beszel_hc_shim_host_dir="/opt/shim",
        gatus_image="gatus:test",
        gatus_internal_port=8080,
        gatus_host_config_dir="/etc/catena/gatus",
        gatus_host_localhost_port=18080,
        gatus_network_alias="gatus",
    )
    variables.update(extra)
    return yaml.safe_load(_env().from_string((INFRA / name).read_text()).render(**variables))


def test_only_the_panels_gate_sends_the_gate_secret():
    services = _gates()["services"]
    panel_gate = services["oauth2-proxy-catena-admin"]
    assert panel_gate["environment"]["OAUTH2_PROXY_BASIC_AUTH_PASSWORD"] == GATE_SECRET
    assert "--pass-basic-auth=true" in panel_gate["command"]
    for name, svc in services.items():
        if name == "oauth2-proxy-catena-admin":
            continue
        assert "OAUTH2_PROXY_BASIC_AUTH_PASSWORD" not in svc["environment"], (
            f"{name} sends the panel's gate secret to its own upstream")


def test_the_gated_apps_list_names_the_secret_not_its_value():
    """Every loop over oauth2_proxy_apps would otherwise carry the secret into
    its task output."""
    text = (ROLES / "oauth2_proxy" / "defaults" / "main.yml").read_text()
    assert 'basic_auth_password_var: "catena_admin_gate_secret"' in text
    assert "{{ catena_admin_gate_secret" not in text


def test_healthchecks_is_on_its_own_network_only():
    app = _compose(
        "healthchecks.compose.yml.j2",
        healthchecks_image="healthchecks/healthchecks:test",
        catena_public_surface_deferred=False,
        healthchecks_host_localhost_port=18000,
        healthchecks_hostname="healthchecks.acme.test",
        cloudflare_zone="acme.test",
        infrastructure_smtp={"enabled": False, "sender": "no-reply@acme.test"},
        _healthchecks_secrets=[{"name": "healthchecks_secret_key-0", "target": "SECRET_KEY"}],
    )["services"]["app"]
    assert app["environment"]["REMOTE_USER_HEADER"] == "HTTP_X_FORWARDED_EMAIL"
    assert list(app["networks"]) == [HC_NETWORK]


def test_healthchecks_route_is_only_its_gate():
    """Traefik is not on the Healthchecks network, so a route of its own would
    point nowhere; the gate's is the public path."""
    tasks = yaml.safe_load((ROLES / "infrastructure" / "tasks" / "healthchecks.yml").read_text())
    deploy = next(t for t in tasks if t.get("name") == "Healthchecks: deploy as a swarm stack")
    assert deploy["vars"]["svc_domain_host"] == ""


def test_every_caller_of_healthchecks_joins_its_network():
    gates = _gates()
    hc_gate = gates["services"]["oauth2-proxy-healthchecks"]["networks"]
    assert "catena-network" in hc_gate and HC_NETWORK in hc_gate
    assert gates["networks"][HC_NETWORK] == {"external": True}

    gatus = _compose("gatus.compose.yml.j2")
    shim = _compose("beszel-hc-shim.compose.yml.j2")
    for doc in (gatus, shim):
        assert HC_NETWORK in doc["services"]["app"]["networks"]
        assert doc["networks"][HC_NETWORK] == {"external": True}


def test_no_client_facing_gate_joins_the_healthchecks_network():
    for name, svc in _gates()["services"].items():
        if name == "oauth2-proxy-healthchecks":
            continue
        assert HC_NETWORK not in svc["networks"], (
            f"{name} joins the Healthchecks network; only Healthchecks' own "
            f"gate may")


def _beszel_hub() -> dict:
    return _compose(
        "beszel-hub.compose.yml.j2",
        beszel_image="henrygd/beszel:test",
        admin_email="admin@acme.test",
        admin_password="pw",
        catena_public_surface_deferred=False,
        beszel_hostname="beszel.acme.test",
        beszel_hub_network_alias="beszel-hub",
        beszel_hub_internal_port=8090,
        beszel_hub_data_volume="beszel-hub-data",
        beszel_hub_host_localhost_port=18190,
    )


def test_beszels_hub_is_on_its_own_network_only():
    hub = _beszel_hub()
    assert list(hub["services"]["app"]["networks"]) == [BESZEL_NETWORK]
    assert list(hub["networks"]) == [BESZEL_NETWORK]


def test_the_hubs_address_follows_its_hostname():
    """The hub writes APP_URL over its stored meta.appURL on every start, and
    a changed value redeploys it."""
    env = _beszel_hub()["services"]["app"]["environment"]
    assert env["APP_URL"] == "https://beszel.acme.test"


def test_every_caller_of_the_hub_joins_its_network():
    """Its gate, Gatus's probe and the panel's API reads reach the hub, which
    calls the alert shim."""
    hub_gate = _gates()["services"]["oauth2-proxy-beszel"]["networks"]
    assert "catena-network" in hub_gate and BESZEL_NETWORK in hub_gate
    assert BESZEL_NETWORK in _compose("gatus.compose.yml.j2")["services"]["app"]["networks"]
    deploy = (ROLES / "catena-admin" / "tasks" / "deploy.yml").read_text()
    assert '- "{{ beszel_network }}"' in deploy


def test_the_alert_shim_is_reached_by_the_hub_alone():
    """Any request the shim takes becomes a Healthchecks ping, so no client
    app reaches it."""
    shim = _compose("beszel-hc-shim.compose.yml.j2")["services"]["app"]["networks"]
    assert set(shim) == {BESZEL_NETWORK, HC_NETWORK}


def test_no_client_facing_gate_joins_the_beszel_network():
    for name, svc in _gates()["services"].items():
        if name == "oauth2-proxy-beszel":
            continue
        assert BESZEL_NETWORK not in svc["networks"], (
            f"{name} joins the Beszel network; only the hub's own gate may")


def test_the_swarm_role_creates_both_networks():
    tasks = yaml.safe_load((ROLES / "swarm" / "tasks" / "main.yml").read_text())
    create = next(t for t in tasks if "private overlays" in t.get("name", ""))
    assert create["loop"] == ["{{ healthchecks_network }}", "{{ beszel_network }}"]
    assert "--attachable" not in create["ansible.builtin.command"]["argv"]
