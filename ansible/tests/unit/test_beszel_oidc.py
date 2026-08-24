"""Beszel's own login gets SSO, and keeps its password path.

Beszel already sits behind oauth2-proxy (vps.auth.mode=admin-only), so whoever
reaches its login form has authenticated to Keycloak and proved `admin` group
membership -- and was then asked for a password a second time by PocketBase,
for access already granted. The OIDC provider turns that prompt into a button.

TWO PROPERTIES, and the second is the one that needs a test more than the
first:

1. the provider is configured with the field names PocketBase actually reads
2. password login is NOT disabled

(2) matters because Beszel is base-plane infrastructure rather than a client
app. A monitoring tool reachable only through the SSO tool cannot be used to
diagnose the SSO tool -- the same circular dependency that moved catena-admin
off a Portainer stack deploy. It is also load-bearing for beszel-seed.py
itself, which authenticates against `_superusers`.

Run: uv run pytest tests/unit/test_beszel_oidc.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
SEED = ANSIBLE / "scripts" / "beszel-seed.py"
BESZEL_TASKS = ANSIBLE / "roles" / "infrastructure" / "tasks" / "beszel.yml"
HUB_COMPOSE = (ANSIBLE / "roles" / "infrastructure" / "templates"
               / "beszel-hub.compose.yml.j2")
REALM = ANSIBLE / "roles" / "keycloak" / "templates" / "realm-beszel.yaml.j2"

# The script's filename carries a hyphen, so it is loaded by path rather than
# imported by name.
_spec = importlib.util.spec_from_file_location("beszel_seed", SEED)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


# ─── the password path stays ────────────────────────────────────────────────

def test_password_login_is_never_disabled():
    """The whole reason this is safe to add. DISABLE_PASSWORD_AUTH would make
    the resource monitor unreachable exactly when Keycloak is the thing that
    is broken -- and would also cut beszel-seed.py's own superuser login if
    Beszel ever widens the flag past the `users` collection."""
    # Matches an ASSIGNMENT (`NAME: value` in compose, `NAME=value` in the env
    # line), not a mention. The prose in these files explains at length why the
    # flag is not set, and a test that forbade naming it would push out the
    # explanation and keep only the rule.
    assigned = re.compile(r"DISABLE_PASSWORD_AUTH\s*[:=]")
    for path in (HUB_COMPOSE, BESZEL_TASKS, SEED):
        offenders = [ln for ln in path.read_text().splitlines()
                     if assigned.search(ln) and not ln.lstrip().startswith("#")]
        assert not offenders, (
            f"{path.name} sets DISABLE_PASSWORD_AUTH: {offenders}. Beszel is "
            "base-plane infrastructure; the local login is the break-glass for "
            "a Keycloak outage")


def test_the_seed_still_authenticates_as_superuser():
    """`_superusers` is a different collection from the `users` one OIDC
    governs, which is why enabling OIDC does not break the seed. If this ever
    moves to the users collection, DISABLE_PASSWORD_AUTH stops being a free
    choice and this file's reasoning no longer holds."""
    assert "/api/collections/_superusers/auth-with-password" in SEED.read_text()


# ─── the provider is written with names PocketBase reads ───────────────────

def test_the_provider_uses_the_vendored_pocketbase_field_names():
    """core.OAuth2ProviderConfig tags them authURL / tokenURL / userInfoURL.
    The pre-v0.23 spelling was authUrl / tokenUrl / userApiUrl, which still
    appears in search results and older migration snippets -- and PocketBase
    DROPS unknown keys rather than rejecting them, so the wrong spelling
    produces a provider that saves cleanly and cannot complete a login."""
    desired = seed._desired_provider({
        "client_id": "beszel", "client_secret": "s",
        "auth_url": "https://auth.example/a", "token_url": "https://auth.example/t",
        "userinfo_url": "https://auth.example/u", "display_name": "Catena",
    })
    assert set(desired) == {
        "name", "clientId", "clientSecret",
        "authURL", "tokenURL", "userInfoURL", "displayName",
    }, f"provider payload keys drifted: {sorted(desired)}"
    for stale in ("authUrl", "tokenUrl", "userApiUrl", "userinfoURL"):
        assert stale not in desired


def test_the_provider_name_is_pocketbases_generic_oidc_key():
    """tools/auth/oidc.go registers exactly `oidc`, `oidc2`, `oidc3`. A name
    outside that set has no factory and the login 404s."""
    assert seed._PROVIDER in ("oidc", "oidc2", "oidc3")


def test_an_absent_secret_on_read_is_not_treated_as_drift():
    """PocketBase tags clientSecret `omitempty`, so a hub that does not return
    it would otherwise make every converge report changed -- which fails the
    bench's strict changed=0 idempotency rerun."""
    desired = seed._desired_provider({
        "client_id": "beszel", "client_secret": "s",
        "auth_url": "a", "token_url": "t", "userinfo_url": "u",
        "display_name": "Catena",
    })
    without_secret = {k: v for k, v in desired.items() if k != "clientSecret"}
    assert seed._provider_matches(without_secret, desired), (
        "a masked secret reads as drift; the converge would never be idempotent")
    # A secret that IS returned and differs must still be a write.
    rotated = dict(desired, clientSecret="other")
    assert not seed._provider_matches(rotated, desired)
    # A real config difference is always a write.
    assert not seed._provider_matches(dict(desired, clientId="nope"), desired)


def test_other_providers_are_preserved(monkeypatch):
    """A client who wired up GitHub login by hand must not lose it because the
    converge reconciled ours. Merge on name, never overwrite the list."""
    calls = {}

    def fake_req(method, url, *, token=None, body=None):
        if method == "GET":
            return {"oauth2": {"enabled": True, "providers": [
                {"name": "github", "clientId": "gh"},
                {"name": "oidc", "clientId": "stale", "authURL": "old",
                 "tokenURL": "t", "userInfoURL": "u", "displayName": "Catena"},
            ]}} if "after" not in calls else calls["after"]
        calls["body"] = body
        calls["after"] = {"oauth2": {"enabled": True, "providers": body["oauth2"]["providers"]}}
        return {}

    monkeypatch.setattr(seed, "_req", fake_req)
    state = seed.configure_oidc("http://h", "tok", {
        "client_id": "beszel", "client_secret": "s", "auth_url": "old",
        "token_url": "t", "userinfo_url": "u", "display_name": "Catena",
    })
    names = [p["name"] for p in calls["body"]["oauth2"]["providers"]]
    assert "github" in names, "a hand-configured provider was dropped"
    assert names.count("oidc") == 1, f"duplicate oidc entries: {names}"
    assert state == "updated"


def test_a_dropped_key_on_read_back_fails_loudly(monkeypatch):
    """The read-back is the only thing that turns a silent no-op into a
    converge failure. Without it a future PocketBase rename ships a button
    that 400s."""
    def fake_req(method, url, *, token=None, body=None):
        if method == "GET":
            # The hub answers as if it never stored authURL.
            return {"oauth2": {"enabled": True, "providers": [
                {"name": "oidc", "clientId": "beszel", "authURL": "",
                 "tokenURL": "t", "userInfoURL": "u", "displayName": "Catena"},
            ]}}
        return {}

    monkeypatch.setattr(seed, "_req", fake_req)
    with pytest.raises(SystemExit):
        seed.configure_oidc("http://h", "tok", {
            "client_id": "beszel", "client_secret": "s",
            "auth_url": "https://auth.example/a", "token_url": "t",
            "userinfo_url": "u", "display_name": "Catena",
        })


# ─── the Keycloak client + the wiring ──────────────────────────────────────

def test_the_realm_client_redirect_is_pocketbases_fixed_callback():
    """PocketBase's callback path is /api/oauth2-redirect and is not
    configurable in Beszel. A mismatch is an invalid_redirect_uri at Keycloak,
    which reads like a Keycloak problem."""
    body = REALM.read_text()
    assert "/api/oauth2-redirect" in body
    assert "{{ beszel_hostname }}" in body, (
        "the redirect hardcodes a hostname instead of following "
        "BESZEL_SUBDOMAIN; renaming the subdomain would break login")
    assert "{{ beszel_oidc_client_secret }}" in body, (
        "the client secret is not read from the on-box store")


def test_the_client_secret_is_store_minted():
    """Every other OIDC client secret is minted in onbox_config's table so it
    survives a restore. One that is not would be regenerated into a mismatch."""
    body = (ANSIBLE / "helpers" / "onbox_config.py").read_text()
    assert re.search(r'"beszel_oidc_client_secret":\s*mint_', body), (
        "beszel_oidc_client_secret is not in the mint table")


def test_the_realm_is_imported_before_the_provider_is_configured():
    """The seed points PocketBase at a Keycloak client. Configuring the
    provider before the realm carries that client leaves a button that 400s
    until the next converge."""
    tasks = [t for t in yaml.safe_load(BESZEL_TASKS.read_text()) if isinstance(t, dict)]
    names = [str(t.get("name", "")) for t in tasks]
    render = next(i for i, n in enumerate(names) if "render Keycloak client" in n)
    seed_idx = next(i for i, n in enumerate(names) if "seed the universal token" in n)
    assert render < seed_idx, (
        "the OIDC provider is configured before its Keycloak client exists")


def test_a_half_changed_seed_still_reports_changed():
    """The script prints both outcomes on one line. The old substring match on
    'minted'/'updated' would miss "token already current, OIDC newly
    configured" -- a real write reported as no change, which is how a converge
    stops being trustworthy."""
    tasks = [t for t in yaml.safe_load(BESZEL_TASKS.read_text()) if isinstance(t, dict)]
    task = next(t for t in tasks if "seed the universal token" in str(t.get("name", "")))
    assert "ok-exists" in str(task["changed_when"]), (
        f"changed_when is {task['changed_when']!r}; it must key on BOTH halves "
        "reporting no-op, not on one substring")
