"""Each admin tool's sign-in gate keeps a session cookie of its own on its own
host, and reads its credentials from a swarm secret rather than its service
environment.

A gate forwards the browser's cookies to the app behind it. So each gate this
role renders names its cookie after its slug, signs it with a secret of its
own and sets no cookie domain: the browser sends it to that host alone, and a
cookie minted anywhere else fails its signature check (oauth2-proxy signs the
cookie's name with its value, pkg/encryption/utils.go at v7.15.4). Its client
secret, cookie secret and the password it sends its upstream are one config
file per gate, mounted as a swarm secret and read with --config, so `docker
service inspect` shows none of them.

The deploy's set_fact and both templates go through ansible-core's own
templating, the way the converge renders them.

Run: uv run pytest tests/unit/test_admin_tool_gates.py
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "oauth2_proxy"
DEPLOY = ROLE / "tasks" / "deploy.yml"
MAIN = ROLE / "tasks" / "main.yml"
COMPOSE = ROLE / "templates" / "oauth2-proxy.compose.yml.j2"

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
APPS = DEFAULTS["oauth2_proxy_apps"]
TARGET = DEFAULTS["oauth2_proxy_config_target"]
COOKIE_PREFIX = DEFAULTS["oauth2_proxy_cookie_name"]

# Shaped like the store's values, with the characters a JSON string escapes.
CLIENT_SECRET = 'client+secret/with"quote\\=='
GATE_SECRET = "gate+secret/value=="
SHARED_COOKIE_SECRET = "the-client-apps-cookie-secret"


def _onbox_config():
    spec = importlib.util.spec_from_file_location(
        "onbox_config", ANSIBLE / "helpers" / "onbox_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cookie_secrets(**override) -> dict:
    out = {a["cookie_secret_var"]: f"cookie-secret-of-{a['slug']}" for a in APPS}
    out.update(override)
    return out


def _render(text: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(text))


def _secrets(cookie_secrets: dict) -> dict:
    """_oauth2_proxy_secrets, built the way the deploy's set_fact loop builds
    it, one item at a time."""
    expr = yaml.safe_load(DEPLOY.read_text())[0][
        "ansible.builtin.set_fact"]["_oauth2_proxy_secrets"]
    acc: dict = {}
    for app in APPS:
        acc = _render(expr, {
            "_app": app,
            "_oauth2_proxy_secrets": acc,
            "oauth2_proxy_config_target": TARGET,
            "oauth2_proxy_client_secret": CLIENT_SECRET,
            "catena_admin_gate_secret": GATE_SECRET,
            "ansible_search_path": [str(ROLE)],
            **cookie_secrets,
        })
    return acc


def _compose(secrets: dict) -> tuple[str, dict]:
    text = _render(COMPOSE.read_text(), {
        "ansible_managed": "managed",
        "oauth2_proxy_apps": [
            {**a, "networks": ["catena-private"]} if "networks" in a else a
            for a in APPS],
        "oauth2_proxy_image": "quay.io/oauth2-proxy/oauth2-proxy:test",
        "oauth2_proxy_config_target": TARGET,
        "oauth2_proxy_internal_port": 4180,
        "oauth2_proxy_oidc_issuer": "https://auth.acme.test/realms/vps",
        "oauth2_proxy_login_url": "https://auth.acme.test/login",
        "oauth2_proxy_redeem_url": "http://keycloak-server:8080/token",
        "oauth2_proxy_jwks_url": "http://keycloak-server:8080/certs",
        "oauth2_proxy_client_id": "oauth2-proxy",
        "oauth2_proxy_cookie_name": COOKIE_PREFIX,
        "oauth2_proxy_client_secret": CLIENT_SECRET,
        "oauth2_proxy_cookie_secret": SHARED_COOKIE_SECRET,
        "cloudflare_zone": "acme.test",
        "_oauth2_proxy_secrets": secrets,
    })
    return text, yaml.safe_load(text)


def _flags(service: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for arg in service["command"]:
        name, _, value = str(arg).partition("=")
        out.setdefault(name, []).append(value)
    return out


def test_each_gate_names_a_cookie_of_its_own_and_sets_no_cookie_domain():
    _, doc = _compose(_secrets(_cookie_secrets()))
    names = []
    for app in APPS:
        flags = _flags(doc["services"][f"oauth2-proxy-{app['slug']}"])
        assert "--cookie-domain" not in flags, (
            f"{app['slug']}'s gate sets a cookie domain, so every host under it "
            f"receives its session cookie")
        assert flags["--cookie-name"] == [f"{COOKIE_PREFIX}_{app['slug']}"]
        names += flags["--cookie-name"]
    assert len(set(names)) == len(APPS)


def test_each_gate_signs_its_cookie_with_a_secret_of_its_own():
    oc = _onbox_config()
    owned = [a["cookie_secret_var"] for a in APPS]
    assert len(set(owned)) == len(APPS)
    assert "oauth2_proxy_cookie_secret" not in owned
    for key in owned:
        assert oc.INTERNAL_SECRETS.get(key) is oc.mint_oauth2_proxy_cookie_secret, (
            f"{key} is not minted as an oauth2-proxy cookie secret")
    secrets = _secrets(_cookie_secrets())
    for app in APPS:
        cfg = tomllib.loads(secrets[app["slug"]]["value"])
        assert cfg["cookie_secret"] == f"cookie-secret-of-{app['slug']}"


def test_the_service_spec_carries_no_credential():
    text, doc = _compose(_secrets(_cookie_secrets()))
    for value in (CLIENT_SECRET, GATE_SECRET, SHARED_COOKIE_SECRET,
                  *_cookie_secrets().values()):
        assert value not in text, f"{value!r} is in the rendered stack"
    for app in APPS:
        svc = doc["services"][f"oauth2-proxy-{app['slug']}"]
        assert "environment" not in svc
        assert _flags(svc)["--config"] == [f"/run/secrets/{TARGET}"]
        [mount] = svc["secrets"]
        assert mount["target"] == TARGET
        assert doc["secrets"][mount["source"]] == {"external": True}


def test_each_gate_mounts_its_own_credentials_file():
    secrets = _secrets(_cookie_secrets())
    _, doc = _compose(secrets)
    for app in APPS:
        [mount] = doc["services"][f"oauth2-proxy-{app['slug']}"]["secrets"]
        assert mount["source"] == secrets[app["slug"]]["name"]
        assert secrets[app["slug"]]["target"] == TARGET


def test_the_credentials_file_is_the_gates_config():
    """oauth2-proxy reads client_secret, cookie_secret and basic_auth_password
    from its --config file (TOML, pkg/apis/options legacy cfg tags)."""
    secrets = _secrets(_cookie_secrets())
    for app in APPS:
        cfg = tomllib.loads(secrets[app["slug"]]["value"])
        want = {"client_secret": CLIENT_SECRET,
                "cookie_secret": f"cookie-secret-of-{app['slug']}"}
        if "basic_auth_password_var" in app:
            want["basic_auth_password"] = GATE_SECRET
        assert cfg == want, app["slug"]


def test_a_rotated_cookie_secret_is_a_new_secret_for_that_gate_alone():
    """Swarm secrets are immutable, so a gate follows a rotation only through
    a new name, which redeploys it."""
    before = _secrets(_cookie_secrets())
    after = _secrets(_cookie_secrets(catena_admin_gate_cookie_secret="rotated"))
    changed = sorted(s for s in before if before[s]["name"] != after[s]["name"])
    assert changed == ["catena-admin"]


def test_the_converge_creates_the_secrets_before_the_deploy():
    tasks = yaml.safe_load(DEPLOY.read_text())
    names = [t.get("name", "") for t in tasks]
    create = names.index("oauth2-proxy: create the missing swarm secrets")
    assert names.index("oauth2-proxy: name each instance's credentials file "
                       "after its content") < create
    assert create < names.index("oauth2-proxy: deploy per-app instance stack")
    assert tasks[create]["ansible.builtin.include_tasks"].endswith(
        "/tasks/swarm_secrets.yml")
    assert tasks[create]["vars"]["swarm_secrets"] == (
        "{{ _oauth2_proxy_secrets.values() | list }}")


def test_the_preflight_checks_every_gates_cookie_secret():
    tasks = yaml.safe_load(MAIN.read_text())
    check = next(t for t in tasks
                 if t.get("name") == "oauth2-proxy: preflight each instance's cookie secret")
    assert check["loop"] == "{{ oauth2_proxy_apps }}"
    for value, ok in (("a" * 43, True), ("a" * 22 + "==", True), ("a" * 44, False), ("", False)):
        result = _render(
            "{{ " + check["ansible.builtin.assert"]["that"][0] + " }}",
            {"_app": {"cookie_secret_var": "k"}, "k": value})
        assert result is ok, value
