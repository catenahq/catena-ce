"""Unit tests for clients_provisioner per-zone SSO islands (multi-domain).

Run: uv run pytest tests/unit/test_clients_provisioner_multidomain.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "ansible" / "scripts"
_spec = importlib.util.spec_from_file_location(
    "clients_provisioner", _SCRIPTS / "clients_provisioner.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _base_env():
    return {
        "OAUTH2_PROXY_INTERNAL_PORT": "4180",
        "OAUTH2_PROXY_IMAGE": "img:pin",
        "OAUTH2_PROXY_COOKIE_NAME": "_catena",
        "OAUTH2_PROXY_CLIENT_ID": "oauth2-proxy",
        "OAUTH2_PROXY_CLIENT_SECRET": "clientsecret",
        "OAUTH2_PROXY_COOKIE_SECRET": "PRIMARYSECRET",
        "OAUTH2_PROXY_OIDC_ISSUER": "https://auth.a.com/realms/vps",
        "OAUTH2_PROXY_LOGIN_URL": "https://auth.a.com/realms/vps/protocol/openid-connect/auth",
        "OAUTH2_PROXY_REDEEM_URL": "http://keycloak-server:8080/realms/vps/protocol/openid-connect/token",
        "OAUTH2_PROXY_JWKS_URL": "http://keycloak-server:8080/realms/vps/protocol/openid-connect/certs",
        "CLOUDFLARE_ZONE": "a.com",
        "AUTH_SUBDOMAIN": "auth",
        "KEYCLOAK_REALM": "vps",
    }


def _spec_for(slug, host):
    return {"slug": slug, "host": host, "upstream_alias": slug,
            "upstream_port": 80, "allowed_groups": ["staff"]}


def test_single_domain_uses_env_defaults():
    env = _base_env()  # no CLOUDFLARE_ZONES -> single primary zone
    out = cp.build_clients_compose([_spec_for("appa", "appa.a.com")], env)
    assert '--cookie-domain=.a.com' in out
    assert '--whitelist-domain=.a.com' in out
    assert '--oidc-issuer-url=https://auth.a.com/realms/vps' in out
    assert '--login-url=https://auth.a.com/realms/vps/protocol/openid-connect/auth' in out
    assert 'OAUTH2_PROXY_COOKIE_SECRET: "PRIMARYSECRET"' in out


def test_secondary_zone_islanded():
    env = _base_env()
    env["CLOUDFLARE_ZONES"] = '["a.com", "b.com"]'
    env["OAUTH2_PROXY_ZONE_COOKIE_SECRETS"] = '{"b.com": "BSECRET"}'
    out = cp.build_clients_compose(
        [_spec_for("appa", "appa.a.com"), _spec_for("appb", "appb.b.com")], env)
    # Primary app keeps the env-default island.
    assert '--cookie-domain=.a.com' in out
    assert 'OAUTH2_PROXY_COOKIE_SECRET: "PRIMARYSECRET"' in out
    # Secondary app is islanded on auth.b.com with its own cookie secret.
    assert '--cookie-domain=.b.com' in out
    assert '--whitelist-domain=.b.com' in out
    assert '--oidc-issuer-url=https://auth.b.com/realms/vps' in out
    assert '--login-url=https://auth.b.com/realms/vps/protocol/openid-connect/auth' in out
    assert 'OAUTH2_PROXY_COOKIE_SECRET: "BSECRET"' in out


def test_secondary_zone_without_zone_secret_falls_back():
    env = _base_env()
    env["CLOUDFLARE_ZONES"] = '["a.com", "b.com"]'
    # No OAUTH2_PROXY_ZONE_COOKIE_SECRETS -> fall back to the base cookie secret
    # (still islanded on the b.com issuer/cookie-domain).
    out = cp.build_clients_compose([_spec_for("appb", "appb.b.com")], env)
    assert '--cookie-domain=.b.com' in out
    assert '--oidc-issuer-url=https://auth.b.com/realms/vps' in out
    assert 'OAUTH2_PROXY_COOKIE_SECRET: "PRIMARYSECRET"' in out


def test_app_zone_longest_suffix_match():
    zones = ["a.com", "sub.a.com"]
    assert cp._app_zone("x.sub.a.com", zones, "a.com") == "sub.a.com"
    assert cp._app_zone("x.a.com", zones, "a.com") == "a.com"
    assert cp._app_zone("x.unknown.net", zones, "a.com") == "a.com"
