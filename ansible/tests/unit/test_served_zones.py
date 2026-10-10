"""The domains a host serves reach both renders that island them.

playbooks/tasks/served_zones.yml reads the tunnel engine's projection
(/var/lib/catena/cloudflare-zones.json) into catena_secondary_zones, and two
renders read that one value: reconcile/roles/keycloak's compose and auth route,
and reconcile/roles/infrastructure's dashboard-sync env, whose CLOUDFLARE_ZONES
and per-domain cookie secrets catena-admin payload/lib/clients_provisioner.py
islands each gated app's oauth2-proxy on. Each consumer imports the reader
right before it renders. Whatever the domains, the same env sends
dashboard-sync's Keycloak calls to catena-admin on the host's loopback. Every
domain's sign-in host is one of the addresses Catena itself answers on
(catena_hostnames), the list dashboard-sync's env and the panel's both carry,
which reconcile/roles/catena-admin reads the projection for too.

The renders go through ansible-core's own templating, with the reader's
set_fact expression evaluated as written, so a test here fails the way a
converge would.

Run: uv run pytest tests/unit/test_served_zones.py
"""
from __future__ import annotations

import base64
import importlib.util
import json
import shlex
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[2]
READER = ANSIBLE / "playbooks" / "tasks" / "served_zones.yml"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
KC_DEPLOY = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "deploy.yml"
KC_COMPOSE = ANSIBLE / "reconcile" / "roles" / "keycloak" / "templates" / "keycloak.compose.yml.j2"
SYNC_TASKS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks" / "dashboard_sync.yml"
SYNC_ENV = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "templates" / "dashboard-sync.env.j2"
GATES = ANSIBLE / "reconcile" / "roles" / "oauth2_proxy" / "defaults" / "main.yml"
PANEL_DEPLOY = ANSIBLE / "reconcile" / "roles" / "catena-admin" / "tasks" / "deploy.yml"
_IMPORT = "{{ playbook_dir }}/tasks/served_zones.yml"

# The filter the reader calls lives beside the playbooks.
add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

PRIMARY, SECOND = "one.example", "two.example"
NOW = "2026-10-07T12:00:00Z"


def _projection(*zones: str) -> dict:
    """What the reader's slurp registers for a projection naming `zones`."""
    body = json.dumps({
        "schema": 1,
        "generated_at": "2026-10-07T11:30:00Z",
        "zones": [{"zone": z, "account_id": "acc"} for z in zones],
    })
    return {"content": base64.b64encode(body.encode()).decode(), "encoding": "base64"}


# What the slurp registers, under failed_when: false, when there is no file.
_NO_FILE = {"failed": False, "msg": "file not found: /var/lib/catena/cloudflare-zones.json"}

# (primary domain, what the slurp registered, the domains dashboard-sync is
# given, whether Keycloak derives its hostname from the request)
CASES = {
    "two attached domains": (PRIMARY, _projection(PRIMARY, SECOND), [PRIMARY, SECOND], True),
    "one domain": (PRIMARY, _projection(PRIMARY), [PRIMARY], False),
    "no projection file": (PRIMARY, _NO_FILE, [PRIMARY], False),
    "no domain yet": ("", _NO_FILE, [], True),
}


def _onbox_config():
    spec = importlib.util.spec_from_file_location(
        "onbox_config", ANSIBLE / "helpers" / "onbox_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _render(text: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(text))


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _index(tasks: list[dict], fragment: str) -> int:
    return next(i for i, t in enumerate(tasks) if fragment in (t.get("name") or ""))


def _shared() -> dict:
    return yaml.safe_load(GROUP_VARS.read_text())


def _secondaries(registered: dict) -> list[str]:
    """catena_secondary_zones, as the reader's set_fact computes it."""
    tasks = _tasks(READER)
    expr = tasks[_index(tasks, "resolve the secondary domains")][
        "ansible.builtin.set_fact"]["catena_secondary_zones"]
    shared = _shared()
    return _render(expr, {
        "_served_zones_raw": registered,
        "ansible_date_time": {"iso8601": NOW},
        "catena_zone_projection_schema": shared["catena_zone_projection_schema"],
        "catena_zone_projection_max_age_hours":
            shared["catena_zone_projection_max_age_hours"],
    })


def _hostnames(zone: str) -> dict:
    """Each of Catena's own hostname variables, a name of its own on `zone`."""
    return {
        "keycloak_hostname": f"auth.{zone}",
        "coturn_hostname": f"turn.{zone}",
        "catena_admin_hostname": f"dash.{zone}",
        "infrastructure_gatus_hostname": f"gatus.{zone}",
        "healthchecks_hostname": f"healthchecks.{zone}",
        "portainer_admin_hostname": f"portainer.{zone}",
        "beszel_hostname": f"beszel.{zone}",
    }


def _catena_hostnames(zone: str, secondaries: list[str]) -> list[str]:
    """catena_hostnames, as playbooks/group_vars/all/main.yml computes it."""
    return _render(_shared()["catena_hostnames"], {
        **_hostnames(zone),
        "keycloak_subdomain": "auth",
        "catena_secondary_zones": secondaries,
        "oauth2_proxy_apps": yaml.safe_load(GATES.read_text())["oauth2_proxy_apps"],
    })


def _sync_env(zone: str, secondaries: list[str], secrets: dict | None = None) -> str:
    return _render(SYNC_ENV.read_text(), {
        "ansible_managed": "managed",
        "portainer_api_base_onbox": "http://127.0.0.1:9000/api",
        "portainer_api_key": "ptr-key",
        "traefik_dynamic_dir": "/etc/catena/traefik/dynamic",
        **_hostnames(zone),
        "catena_hostnames": _catena_hostnames(zone, secondaries),
        "gatus_compose_name": "gatus",
        "healthchecks_compose_name": "healthchecks",
        "keycloak_compose_name": "keycloak",
        "beszel_hub_compose_name": "beszel-hub",
        "cloudflare_zone": zone,
        "keycloak_subdomain": "auth",
        "keycloak_realm": "vps",
        "oauth2_proxy_image": "quay.io/oauth2-proxy/oauth2-proxy:v7.12.0",
        "oauth2_proxy_internal_port": 4180,
        "oauth2_proxy_cookie_name": "_oauth2_proxy",
        "oauth2_proxy_oidc_issuer": f"https://auth.{zone}/realms/vps",
        "oauth2_proxy_login_url": f"https://auth.{zone}/realms/vps/protocol/openid-connect/auth",
        "oauth2_proxy_redeem_url": "http://keycloak-server:8080/realms/vps/protocol/openid-connect/token",
        "oauth2_proxy_jwks_url": "http://keycloak-server:8080/realms/vps/protocol/openid-connect/certs",
        "oauth2_proxy_client_id": "oauth2-proxy",
        "oauth2_proxy_client_secret": "client-secret",
        "oauth2_proxy_cookie_secret": "primary-cookie-secret",
        "catena_admin_ui_port": "9010",
        "catena_marketplace_token": "market-token",
        "catena_secondary_zones": secondaries,
        **(secrets or {}),
    })


def _env_value(rendered: str, key: str) -> str:
    """KEY's value as a shell sourcing the file reads it. The line has to be ONE
    shell word, or the shell runs the rest of it as a command; systemd's
    EnvironmentFile reads a single-quoted value the same way, verbatim."""
    line = next(ln for ln in rendered.splitlines() if ln.startswith(key + "="))
    words = shlex.split(line)
    assert len(words) == 1, f"{key} splits into {len(words)} shell words: {line!r}"
    return words[0].split("=", 1)[1]


def _keycloak_env(zone: str, secondaries: list[str]) -> dict:
    doc = yaml.safe_load(_render(KC_COMPOSE.read_text(), {
        "ansible_managed": "managed",
        "keycloak_server_service": "server",
        "keycloak_image": "quay.io/phasetwo/phasetwo-keycloak:26.6.7.1790010472",
        "keycloak_db_name": "keycloak",
        "keycloak_db_user": "keycloak",
        "keycloak_config_target": "keycloak.conf",
        "_keycloak_secrets": [{"name": "keycloak-conf-0123abcd"}],
        "keycloak_http_enabled": True,
        "catena_secondary_zones": secondaries,
        "catena_public_surface_deferred": not zone,
        "keycloak_hostname": f"auth.{zone}",
        "keycloak_proxy_headers": "xforwarded",
        "admin_email": f"admin@{PRIMARY}",
        "keycloak_server_alias": "keycloak-server",
        "keycloak_management_port": 9000,
        "keycloak_health_path": "/health/ready",
    }))
    return doc["services"]["server"]["environment"]


@pytest.mark.parametrize("case", CASES, ids=list(CASES))
def test_dashboard_sync_is_given_every_served_domain(case):
    zone, registered, expected, _ = CASES[case]
    rendered = _sync_env(zone, _secondaries(registered))
    assert json.loads(_env_value(rendered, "CLOUDFLARE_ZONES")) == expected
    assert _env_value(rendered, "CLOUDFLARE_ZONE") == zone


@pytest.mark.parametrize("case", CASES, ids=list(CASES))
def test_keycloak_takes_its_hostname_from_the_request_on_a_multi_domain_host(case):
    """A fixed KC_HOSTNAME mints every issuer on the primary's auth host, which
    the second domain's oauth2-proxy refuses."""
    zone, registered, _, request_derived = CASES[case]
    env = _keycloak_env(zone, _secondaries(registered))
    if request_derived:
        assert "KC_HOSTNAME" not in env
        assert env["KC_HOSTNAME_STRICT"] == "false"
    else:
        assert env["KC_HOSTNAME"] == f"https://auth.{zone}"
        assert env["KC_HOSTNAME_STRICT"] == "true"


@pytest.mark.parametrize("registered, expected", [
    (_projection(PRIMARY, SECOND), [f"auth.{SECOND}"]),
    (_projection(PRIMARY), []),
    (_NO_FILE, []),
], ids=["two attached domains", "one domain", "no projection file"])
def test_keycloak_routes_each_secondary_auth_host(registered, expected):
    tasks = _tasks(KC_DEPLOY)
    expr = tasks[_index(tasks, "deploy as a swarm stack")]["vars"]["svc_domain_extra_hosts"]
    hosts = _render(expr, {
        "catena_secondary_zones": _secondaries(registered),
        "keycloak_subdomain": "auth",
    })
    assert hosts == expected


def test_only_a_secondary_domain_gets_a_cookie_secret_of_its_own():
    """The store mints one per domain it lists, the primary's included
    (helpers/onbox_config.py); clients_provisioner signs a secondary domain's
    sessions with its own and the primary's with OAUTH2_PROXY_COOKIE_SECRET.
    The variable names are the store keys, so the template and the minter
    agree on the slug."""
    key = _onbox_config().zone_cookie_secret_key
    secrets = {key(PRIMARY): "s1", key(SECOND): "s2"}
    two = _sync_env(PRIMARY, _secondaries(_projection(PRIMARY, SECOND)), secrets)
    assert json.loads(_env_value(two, "OAUTH2_PROXY_ZONE_COOKIE_SECRETS")) == {SECOND: "s2"}
    one = _sync_env(PRIMARY, _secondaries(_projection(PRIMARY)), secrets)
    assert json.loads(_env_value(one, "OAUTH2_PROXY_ZONE_COOKIE_SECRETS")) == {}


@pytest.mark.parametrize("case", CASES, ids=list(CASES))
def test_dashboard_sync_reaches_keycloak_through_the_panel_on_loopback(case):
    """dashboard-sync runs on the host, where keycloak-server does not resolve,
    and the public auth host is a round trip through Cloudflare and the tunnel.
    catena-admin forwards the calls on its published loopback port, behind the
    token the managed env uses, on every host whatever domains it serves."""
    zone, registered, _, _ = CASES[case]
    rendered = _sync_env(zone, _secondaries(registered))
    base = "http://127.0.0.1:9010/keycloak"
    assert _env_value(rendered, "KEYCLOAK_TOKEN_URL") == (
        f"{base}/realms/vps/protocol/openid-connect/token")
    assert _env_value(rendered, "KEYCLOAK_CLIENTS_API") == f"{base}/admin/realms/vps/clients"
    assert _env_value(rendered, "CATENA_MANAGED_ENV_URL") == (
        "http://127.0.0.1:9010/marketplace/managed-env.json")
    assert _env_value(rendered, "CATENA_MARKETPLACE_TOKEN") == "market-token"


def test_the_marketplace_token_is_in_no_url():
    """dashboard-sync prints a failed call's URL to its journal, which the
    converge log and the panel's Sync all output copy. The token rides its own
    line, which the lane sends as a request header (catena-admin
    payload/lib/portainer_api.py panel_headers)."""
    rendered = _sync_env(PRIMARY, [])
    carrying = [ln.split("=", 1)[0] for ln in rendered.splitlines()
                if "market-token" in ln and not ln.startswith("#")]
    assert carrying == ["CATENA_MARKETPLACE_TOKEN"]


def test_no_marketplace_token_means_no_managed_env_url():
    """A host whose converge has not minted the token yet has nothing the
    panel would answer, so the lane reconciles nothing rather than failing on
    every run."""
    rendered = _sync_env(PRIMARY, [], {"catena_marketplace_token": ""})
    assert _env_value(rendered, "CATENA_MANAGED_ENV_URL") == ""
    assert _env_value(rendered, "CATENA_MARKETPLACE_TOKEN") == ""


def test_catena_hostnames_is_every_address_catena_answers_on():
    """Each served domain's sign-in host, the TURN relay and the host of every
    gated tool, the panel's among them. dashboard-sync routes no client app on
    one (catena-admin payload/lib/app_intent.py), and the app checker holds them
    as Catena's on each app's tile and in the panel's Check tab, so
    dashboard-sync and the panel are each given the one list."""
    hosts = _catena_hostnames(PRIMARY, [SECOND])
    names = _hostnames(PRIMARY)
    assert sorted(hosts) == sorted([*names.values(), f"auth.{SECOND}"])
    gates = yaml.safe_load(GATES.read_text())["oauth2_proxy_apps"]
    assert {names[g["host_var"]] for g in gates} <= set(hosts)
    synced = json.loads(_env_value(_sync_env(PRIMARY, [SECOND]), "CATENA_HOSTNAMES"))
    tasks = _tasks(PANEL_DEPLOY)
    env = tasks[_index(tasks, "build the service spec")]["ansible.builtin.set_fact"]["_ca_spec"]["env"]
    panel = json.loads(_render(env["CATENA_HOSTNAMES"], {"catena_hostnames": hosts}))
    assert synced == panel == hosts


@pytest.mark.parametrize("path, render", [
    (KC_DEPLOY, "deploy as a swarm stack"),
    (SYNC_TASKS, "env file"),
    (PANEL_DEPLOY, "build the service spec"),
], ids=["keycloak", "dashboard-sync", "panel"])
def test_each_consumer_reads_the_projection_right_before_it_renders(path, render):
    """A converge scoped with --tags to any one of their roles still reads the
    projection."""
    tasks = _tasks(path)
    reader = next(i for i, t in enumerate(tasks)
                  if t.get("ansible.builtin.import_tasks") == _IMPORT)
    assert reader < _index(tasks, render)


def test_a_missing_projection_does_not_fail_the_converge():
    """A missing file is every single-domain host."""
    tasks = _tasks(READER)
    read = tasks[_index(tasks, "read the zone projection")]
    assert read["ansible.builtin.slurp"]["src"] == "{{ catena_zone_projection_path }}"
    assert read["failed_when"] is False


def test_the_projection_contract_is_the_engines():
    shared = _shared()
    assert shared["catena_zone_projection_path"] == "/var/lib/catena/cloudflare-zones.json"
    # catena-admin payload/engines/cloudflared ProjectionSchema.
    assert shared["catena_zone_projection_schema"] == 1
    assert shared["catena_zone_projection_max_age_hours"] > 0


def test_the_reader_carries_no_licence_semantics():
    """The projection already reflects the subscription: the engine capped the
    list before writing it, and the licence has one implementation, in
    catena-admin."""
    for path in (READER, KC_DEPLOY):
        body = path.read_text().lower()
        for name in ("catena_ee_multidomain_enabled", "catena_license", "catena_licence"):
            assert name not in body, f"{path.name} reads {name}"
