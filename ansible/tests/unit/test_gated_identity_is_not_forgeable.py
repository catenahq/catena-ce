"""A client app container cannot claim a signed-in identity at the panel or at
Healthchecks, nor sign in to Beszel's hub.

Both take the signed-in user from headers their oauth2-proxy gate sets, and
every client app joins catena-network, where it can set any header. The panel
therefore takes those headers only beside the gate secret its gate sends as a
Basic password (catena-admin shell/auth Gate). Healthchecks cannot check a
secret, so it is on a network no client app joins. Beszel's hub creates an
account for any realm user who drives its sign-in, so it is on another such
network. It also takes the signed-in email from its gate's header, but only
from peers in the subnet of a third network that only the gate and the hub join.

The gates are rendered the way the converge renders them and read back as YAML,
and a gate's credentials file as the TOML oauth2-proxy reads. The networks the
Healthchecks, Gatus and Beszel apps join are held by the catalog's tests
(catena-templates tests/) and by test_default_apps.py against the wiring
contract.

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

HC_NETWORK = "catena-healthchecks"
BESZEL_NETWORK = "catena-beszel"
GATE_NETWORK = "catena-beszelgate"
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


def test_every_caller_of_healthchecks_joins_its_network():
    gates = _gates()
    hc_gate = gates["services"]["oauth2-proxy-healthchecks"]["networks"]
    assert "catena-network" in hc_gate and HC_NETWORK in hc_gate
    assert gates["networks"][HC_NETWORK] == {"external": True}


def test_no_client_facing_gate_joins_the_healthchecks_network():
    for name, svc in _gates()["services"].items():
        if name == "oauth2-proxy-healthchecks":
            continue
        assert HC_NETWORK not in svc["networks"], (
            f"{name} joins the Healthchecks network; only Healthchecks' own "
            f"gate may")


def test_the_converge_reads_and_checks_the_subnet_before_the_hub_is_added():
    tasks = yaml.safe_load((ROLES / "infrastructure" / "tasks" / "beszel.yml").read_text())
    names = [str(t.get("name", "")) for t in tasks]
    read = next(i for i, n in enumerate(names) if "read the gate network's subnet" in n)
    check = next(i for i, n in enumerate(names) if "exactly one valid subnet" in n)
    pin = next(i for i, n in enumerate(names) if "pin the gate network's subnet" in n)
    values = next(i for i, n in enumerate(names) if "the values the converge owns" in n)
    deploy = next(i for i, n in enumerate(names) if "add the app" in n)
    assert read < check < pin < values < deploy
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


def test_every_caller_of_the_hub_joins_its_network():
    """Its gate reaches the hub on the gate network. The panel reads its gauges
    from the host and links to the hub, so it stays off both."""
    hub_gate = _gates()["services"]["oauth2-proxy-beszel"]["networks"]
    assert "catena-network" in hub_gate and GATE_NETWORK in hub_gate
    assert BESZEL_NETWORK not in hub_gate
    deploy = (ROLES / "catena-admin" / "tasks" / "deploy.yml").read_text()
    assert '- "{{ beszel_network }}"' not in deploy
    assert "beszel_gate_network" not in deploy


def test_no_gate_joins_the_beszel_network():
    for name, svc in _gates()["services"].items():
        assert BESZEL_NETWORK not in svc["networks"], (
            f"{name} joins the Beszel network, where Gatus can reach the hub")


def test_only_the_hubs_gate_and_the_hub_join_the_gate_network():
    """The hub trusts the sign-in header from every peer on this network."""
    for name, svc in _gates()["services"].items():
        if name == "oauth2-proxy-beszel":
            continue
        assert GATE_NETWORK not in svc["networks"], f"{name} joins the gate network"
    allowed = {
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
