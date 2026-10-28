"""A client app container cannot claim a signed-in identity at the panel or at
Healthchecks, nor sign in to Beszel's hub.

Both take the signed-in user from headers their oauth2-proxy gate sets, and
every client app joins catena-network, where it can set any header. The panel
therefore takes those headers only beside the gate secret its gate sends as a
Basic password (catena-admin shell/auth Gate). Healthchecks cannot check a
secret, so it is on a network no client app joins. Beszel's hub creates an
account for any realm user who drives its sign-in, so it is on another such
network, with the alert shim it calls. It also takes the signed-in email from
its gate's header, but only from peers in the subnet of a third network that
only the gate and the hub join.

The compose templates are rendered the way the converge renders them and read
back as YAML, and a gate's credentials file as the TOML oauth2-proxy reads.

Run: uv run pytest tests/unit/test_gated_identity_is_not_forgeable.py
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
INFRA = ROLES / "infrastructure" / "templates"

HC_NETWORK = "catena-healthchecks"
BESZEL_NETWORK = "catena-beszel"
GATE_NETWORK = "catena-beszel-gate"
GATE_SUBNET = "10.0.7.0/24"
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
                                          beszel_network=BESZEL_NETWORK,
                                          beszel_gate_network=GATE_NETWORK)
                for n in app["networks"]
            ]
        apps.append(app)
    return apps


def _gates() -> dict:
    apps = _gated_apps()
    rendered = _env().from_string(
        (ROLES / "oauth2_proxy" / "templates" / "oauth2-proxy.compose.yml.j2").read_text()
    ).render(
        ansible_managed="",
        oauth2_proxy_apps=apps,
        oauth2_proxy_image="quay.io/oauth2-proxy/oauth2-proxy:test",
        oauth2_proxy_config_target="oauth2-proxy.cfg",
        oauth2_proxy_internal_port=4180,
        oauth2_proxy_oidc_issuer="https://auth.acme.test/realms/vps",
        oauth2_proxy_login_url="https://auth.acme.test/login",
        oauth2_proxy_redeem_url="http://keycloak-server:8080/token",
        oauth2_proxy_jwks_url="http://keycloak-server:8080/certs",
        oauth2_proxy_client_id="oauth2-proxy",
        oauth2_proxy_cookie_name="_oauth2_proxy",
        cloudflare_zone="acme.test",
        _oauth2_proxy_secrets={a["slug"]: {"name": f"oauth2-proxy-{a['slug']}-0"}
                               for a in apps},
    )
    return yaml.safe_load(rendered)


def _credentials(app: dict) -> dict:
    """The gate's credentials file, as oauth2-proxy's --config reads it."""
    variables = {"catena_admin_gate_secret": GATE_SECRET,
                 app["cookie_secret_var"]: "ck"}
    rendered = _env().from_string(
        (ROLES / "oauth2_proxy" / "templates" / "oauth2-proxy.cfg.j2").read_text()
    ).render(
        _app=app,
        oauth2_proxy_client_secret="cs",
        lookup=lambda kind, name: variables[name],
    )
    return tomllib.loads(rendered)


def _compose(name: str, **extra) -> dict:
    variables = dict(
        ansible_managed="",
        healthchecks_network=HC_NETWORK,
        beszel_network=BESZEL_NETWORK,
        beszel_gate_network=GATE_NETWORK,
        healthchecks_network_alias="healthchecks",
        healthchecks_internal_port=8000,
        _beszel_hc_shim_secrets=[{"name": "beszel_hc_shim_ping_key-0", "target": "HC_PING_KEY"}],
        beszel_hc_shim_image="python:test",
        beszel_hc_shim_internal_port=8099,
        beszel_hc_shim_network_alias="beszel-hc-shim",
        beszel_hc_shim_host_dir="/opt/shim",
        gatus_image="gatus:test",
        gatus_internal_port=8080,
        gatus_host_config_dir="/etc/catena/gatus",
        gatus_host_localhost_port=18080,
        gatus_network_alias="gatus",
        gatus_data_volume="gatus-data",
        healthchecks_data_volume="healthchecks-data",
        healthchecks_sqlite_file="hc.sqlite",
    )
    variables.update(extra)
    return yaml.safe_load(_env().from_string((INFRA / name).read_text()).render(**variables))


def test_only_the_panels_gate_sends_the_gate_secret():
    services = _gates()["services"]
    assert "--pass-basic-auth=true" in services["oauth2-proxy-catena-admin"]["command"]
    for app in _gated_apps():
        password = _credentials(app).get("basic_auth_password")
        if app["slug"] == "catena-admin":
            assert password == GATE_SECRET
        else:
            assert password is None, (
                f"{app['slug']}'s gate sends the panel's gate secret to its own upstream")


def test_the_gated_apps_list_names_the_secret_not_its_value():
    """Every loop over oauth2_proxy_apps would otherwise carry the secret into
    its task output."""
    text = (ROLES / "oauth2_proxy" / "defaults" / "main.yml").read_text()
    assert 'basic_auth_password_var: "catena_admin_gate_secret"' in text
    assert "{{ catena_admin_gate_secret" not in text
    for app in _gated_apps():
        assert "{{ " + app["cookie_secret_var"] not in text


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


def _beszel_hub(**extra) -> dict:
    return _compose(
        "beszel-hub.compose.yml.j2",
        beszel_image="henrygd/beszel:test",
        catena_public_surface_deferred=False,
        beszel_hostname="beszel.acme.test",
        beszel_hub_network_alias="beszel-hub",
        beszel_hub_internal_port=8090,
        beszel_hub_data_volume="beszel-hub-data",
        beszel_hub_host_localhost_port=18190,
        **extra,
    )


def test_beszels_hub_is_on_its_own_networks_only():
    hub = _beszel_hub(_beszel_gate_subnet=GATE_SUBNET)
    assert list(hub["services"]["app"]["networks"]) == [BESZEL_NETWORK, GATE_NETWORK]
    assert list(hub["networks"]) == [BESZEL_NETWORK, GATE_NETWORK]
    for net in (BESZEL_NETWORK, GATE_NETWORK):
        assert hub["services"]["app"]["networks"][net]["aliases"] == ["beszel-hub"]


def test_the_sign_in_header_is_never_rendered_without_an_allowlist():
    """Beszel trusts every peer when TRUSTED_PROXY_IPS is empty or unset."""
    for subnet in (None, "", "  "):
        extra = {} if subnet is None else {"_beszel_gate_subnet": subnet}
        env = _beszel_hub(**extra)["services"]["app"]["environment"]
        assert "TRUSTED_AUTH_HEADER" not in env and "TRUSTED_PROXY_IPS" not in env, (
            f"rendered with subnet {subnet!r}")
    env = _beszel_hub(_beszel_gate_subnet=GATE_SUBNET)["services"]["app"]["environment"]
    assert env["TRUSTED_AUTH_HEADER"] == "X-Forwarded-Email"
    assert env["TRUSTED_PROXY_IPS"] == GATE_SUBNET


def test_the_converge_reads_and_checks_the_subnet_before_the_hub_deploys():
    tasks = yaml.safe_load((ROLES / "infrastructure" / "tasks" / "beszel.yml").read_text())
    names = [str(t.get("name", "")) for t in tasks]
    read = next(i for i, n in enumerate(names) if "read the gate network's subnet" in n)
    check = next(i for i, n in enumerate(names) if "exactly one valid subnet" in n)
    pin = next(i for i, n in enumerate(names) if "pin the gate network's subnet" in n)
    deploy = next(i for i, n in enumerate(names) if "deploy hub as a swarm stack" in n)
    assert read < check < pin < deploy
    assert tasks[read]["ansible.builtin.command"]["argv"][:3] == ["docker", "network", "inspect"]
    assert "_beszel_gate_subnet" in str(tasks[pin])


def test_the_subnet_check_accepts_one_cidr_and_refuses_the_rest():
    import re
    tasks = yaml.safe_load((ROLES / "infrastructure" / "tasks" / "beszel.yml").read_text())
    check = next(t for t in tasks if "exactly one valid subnet" in str(t.get("name", "")))
    pattern = re.compile(check["vars"]["_beszel_cidr_pattern"])
    assert any("length == 1" in c for c in check["ansible.builtin.assert"]["that"])
    for good in ("10.0.7.0/24", "172.18.0.0/16", "192.168.16.0/20"):
        assert pattern.match(good), good
    for bad in ("", "0.0.0.0/0", "10.0.0.0/0", "10.0.0.0/7", "10.0.0.0/31", "10.0.0.0",
                "10.0.0.256/24", "10.0.0.0/24,10.0.1.0/24", "fd00::/64"):
        assert not pattern.match(bad), bad


def test_the_hubs_address_follows_its_hostname():
    """The hub writes APP_URL over its stored meta.appURL on every start, and
    a changed value redeploys it."""
    env = _beszel_hub()["services"]["app"]["environment"]
    assert env["APP_URL"] == "https://beszel.acme.test"


def test_every_caller_of_the_hub_joins_its_network():
    """Its gate reaches the hub on the gate network, Gatus's probe on the
    other, and the hub calls the alert shim. The panel reads its gauges from
    the host and links to the hub, so it stays off both."""
    hub_gate = _gates()["services"]["oauth2-proxy-beszel"]["networks"]
    assert "catena-network" in hub_gate and GATE_NETWORK in hub_gate
    assert BESZEL_NETWORK not in hub_gate
    assert BESZEL_NETWORK in _compose("gatus.compose.yml.j2")["services"]["app"]["networks"]
    deploy = (ROLES / "catena-admin" / "tasks" / "deploy.yml").read_text()
    assert '- "{{ beszel_network }}"' not in deploy
    assert "beszel_gate_network" not in deploy


def test_the_alert_shim_is_reached_by_the_hub_alone():
    """Any request the shim takes becomes a Healthchecks ping, so no client
    app reaches it."""
    shim = _compose("beszel-hc-shim.compose.yml.j2")["services"]["app"]["networks"]
    assert set(shim) == {BESZEL_NETWORK, HC_NETWORK}


def test_no_gate_joins_the_beszel_network():
    for name, svc in _gates()["services"].items():
        assert BESZEL_NETWORK not in svc["networks"], (
            f"{name} joins the Beszel network, where Gatus and the alert shim "
            f"can reach the hub")


def test_only_the_hubs_gate_and_the_hub_join_the_gate_network():
    """The hub trusts the sign-in header from every peer on this network."""
    for name, svc in _gates()["services"].items():
        if name == "oauth2-proxy-beszel":
            continue
        assert GATE_NETWORK not in svc["networks"], f"{name} joins the gate network"
    allowed = {
        INFRA / "beszel-hub.compose.yml.j2",
        ROLES / "oauth2_proxy" / "defaults" / "main.yml",
        ROLES / "swarm" / "tasks" / "main.yml",
        ROLES / "infrastructure" / "tasks" / "beszel.yml",
    }
    for path in ROLES.rglob("*"):
        if not path.is_file() or path.suffix not in (".yml", ".j2") or path in allowed:
            continue
        assert "beszel_gate_network" not in path.read_text(), (
            f"{path} names the gate network")


def _route_file(app: dict) -> dict:
    """The gate's Traefik route file, rendered the way Ansible renders a
    template (trim_blocks)."""
    env = _env()
    env.trim_blocks = True
    text = (ROLES / "oauth2_proxy" / "templates" / "app-oauth2-proxy.yml.j2").read_text()
    return yaml.safe_load(env.from_string(text).render(
        ansible_managed="", _app=app, _app_hostname="beszel.acme.test",
        oauth2_proxy_internal_port=4180))


def test_the_hub_refuses_api_calls_its_own_page_did_not_make():
    """With the sign-in header trusted, the gate's lax cookie would authorise
    any /api/ call a browser is made to send. A router above the gate's refuses
    those with 403 from Traefik, and the hub's own page (same-origin) passes."""
    app = next(a for a in _gated_apps() if a["slug"] == "beszel")
    doc = _route_file(app)
    routers = doc["http"]["routers"]
    gate, deny = routers["beszel-oauth2-proxy"], routers["beszel-oauth2-proxy-cross-site-api"]
    assert deny["priority"] > gate["priority"]
    assert deny["rule"] == ("Host(`beszel.acme.test`) && PathPrefix(`/api/`) "
                            "&& !Header(`Sec-Fetch-Site`, `same-origin`)")
    assert deny["service"] == "noop@internal"
    assert deny["entryPoints"] == gate["entryPoints"]
    [name] = deny["middlewares"]
    refusal = doc["http"]["middlewares"][name]["ipAllowList"]
    assert refusal == {"sourceRange": ["0.0.0.0/32"], "rejectStatusCode": 403}
    assert gate["service"] in doc["http"]["services"]


def test_only_the_hubs_gate_refuses_cross_site_api_calls():
    for app in _gated_apps():
        doc = _route_file(app)["http"]
        flagged = app["slug"] == "beszel"
        assert (f"{app['slug']}-oauth2-proxy-cross-site-api" in doc["routers"]) is flagged
        assert ("middlewares" in doc) is flagged


def test_the_beszel_agents_do_not_go_through_traefik():
    """The agents dial the hub on the host loopback, so the refusal above does
    not touch them."""
    agent = (INFRA / "beszel-agent.compose.yml.j2").read_text()
    assert "name: host" in agent
    assert 'HUB_URL: "http://127.0.0.1:{{ beszel_hub_host_localhost_port }}"' in agent


def test_the_beszel_gate_strips_the_clients_headers_and_skips_no_auth():
    """oauth2-proxy replaces the sign-in header it sets only while it strips
    the browser's copy."""
    command = " ".join(_gates()["services"]["oauth2-proxy-beszel"]["command"])
    for flag in ("--skip-auth-strip-headers", "--skip-auth-route", "--skip-auth-regex",
                 "--skip-jwt-bearer-tokens"):
        assert flag not in command
    assert "--pass-user-headers=true" in command


def test_the_swarm_role_creates_the_private_networks():
    tasks = yaml.safe_load((ROLES / "swarm" / "tasks" / "main.yml").read_text())
    create = next(t for t in tasks if "private overlays" in t.get("name", ""))
    assert create["loop"] == ["{{ healthchecks_network }}", "{{ beszel_network }}",
                              "{{ beszel_gate_network }}"]
    assert "--attachable" not in create["ansible.builtin.command"]["argv"]
