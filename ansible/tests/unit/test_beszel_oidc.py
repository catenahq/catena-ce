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
BESZEL_TASKS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks" / "beszel.yml"
HUB_COMPOSE = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "templates"
               / "beszel-hub.compose.yml.j2")
REALM = ANSIBLE / "reconcile" / "roles" / "keycloak" / "templates" / "realm-beszel.yaml.j2"

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


# ─── alert delivery + rules ────────────────────────────────────────────────
# Both were manual UI steps. The shim was deployed, no webhook pointed at it and
# no rule would ever fire, so a converged host had a monitor watching a server
# with no way to tell anybody -- the one failure monitoring exists to avoid, and
# invisible precisely because nothing was broken.


def test_the_webhook_points_at_the_shim_this_role_deploys():
    """One definition read by both seed passes. Two would drift, and a
    delivery channel pointed at a service that is not there fails only when it
    matters."""
    tasks = [t for t in yaml.safe_load(BESZEL_TASKS.read_text()) if isinstance(t, dict)]
    fact = next(t for t in tasks if "alert-shim webhook URL" in str(t.get("name", "")))
    url = str(fact["ansible.builtin.set_fact"]["_beszel_alert_webhook"])
    # generic+ is Shoutrrr's raw-webhook scheme; the shim speaks plain HTTP.
    assert url.startswith("generic+http://"), url
    assert "beszel_hc_shim_network_alias" in url
    assert "beszel_hc_shim_internal_port" in url
    assert url.rstrip().endswith("/notify"), url


def test_the_rules_are_seeded_after_the_agent_registers():
    """The rules attach to a SYSTEM, and a system exists only once the agent
    has registered. The agent needs the universal token the first pass seeds,
    so the first pass necessarily runs too early -- which is why there is a
    second one and why it waits rather than assuming."""
    tasks = [t for t in yaml.safe_load(BESZEL_TASKS.read_text()) if isinstance(t, dict)]
    names = [str(t.get("name", "")) for t in tasks]
    first = next(i for i, n in enumerate(names) if "seed the universal token" in n)
    agent = next(i for i, n in enumerate(names) if "deploy agent as a swarm stack" in n)
    rules = next(i for i, n in enumerate(names) if "default alert rules" in n)
    assert first < agent < rules, names

    task = tasks[rules]
    until = str(task["until"])
    assert "no-systems" in until, (
        "the rules pass does not wait for the agent to register; on a fresh "
        "host it would create nothing and report success")


def test_a_never_registering_agent_fails_the_converge():
    """An agent that never registers is a monitor with nothing to watch. It
    must not pass silently just because the seed script exited 0."""
    tasks = [t for t in yaml.safe_load(BESZEL_TASKS.read_text()) if isinstance(t, dict)]
    task = next(t for t in tasks if "default alert rules" in str(t.get("name", "")))
    assert task.get("retries"), "no retry bound: the wait is unbounded or absent"
    assert "failed_when" not in task, (
        "the rules pass swallows its own failure")


def test_both_passes_report_all_four_outcomes():
    """changed_when counts ok-exists across the four steps. Counting three
    would report a converge that newly created every alert rule as unchanged."""
    tasks = [t for t in yaml.safe_load(BESZEL_TASKS.read_text()) if isinstance(t, dict)]
    for fragment in ("seed the universal token", "default alert rules"):
        task = next(t for t in tasks if fragment in str(t.get("name", "")))
        assert "ok-exists') < 4" in str(task["changed_when"]), (
            f"{fragment}: changed_when is {task['changed_when']!r}")


def test_an_operator_added_webhook_is_not_dropped():
    """The converge owns ONE entry in the list, not the list. A client who
    wired up ntfy or Slack in the hub UI keeps it -- same rule the OIDC
    provider merge follows."""
    calls = {}

    def fake_req(method, url, *, token=None, body=None):
        if method == "GET":
            return {"items": [{"id": "us1", "settings": {
                "emails": ["ops@example.test"],
                "webhooks": ["ntfy://example.test/mine"],
            }}]}
        calls["body"] = body
        return {}

    import unittest.mock as _mock
    with _mock.patch.object(seed, "_req", fake_req):
        state = seed.configure_alert_delivery(
            "http://h", "tok", "user1", "generic+http://shim:8080/notify")
    hooks = calls["body"]["settings"]["webhooks"]
    assert "ntfy://example.test/mine" in hooks, "a hand-added webhook was dropped"
    assert "generic+http://shim:8080/notify" in hooks
    assert calls["body"]["settings"]["emails"] == ["ops@example.test"], (
        "email recipients were lost while writing the webhook")
    assert state == "updated"


def test_an_already_present_webhook_is_not_rewritten():
    """Otherwise every converge reports changed and fails the strict
    changed=0 idempotency rerun."""
    def fake_req(method, url, *, token=None, body=None):
        assert method == "GET", f"wrote on a no-op: {method} {url}"
        return {"items": [{"id": "us1", "settings": {
            "webhooks": ["generic+http://shim:8080/notify"]}}]}

    import unittest.mock as _mock
    with _mock.patch.object(seed, "_req", fake_req):
        assert seed.configure_alert_delivery(
            "http://h", "tok", "user1",
            "generic+http://shim:8080/notify") == "ok-exists"


def test_the_default_rules_use_names_the_collection_accepts():
    """`name` is a SELECT on the alerts collection: a value outside its set is
    refused by PocketBase, so these are copied from the collection definition
    rather than invented."""
    allowed = {"Status", "CPU", "Memory", "Disk", "Temperature", "Bandwidth",
               "GPU", "LoadAvg1", "LoadAvg5", "LoadAvg15", "Battery"}
    assert seed._DEFAULT_RULES, "the default rule set is empty"
    for rule in seed._DEFAULT_RULES:
        assert rule["name"] in allowed, rule
        # `min` is MINUTES (internal/alerts/alerts_system.go multiplies it by
        # time.Minute), so a zero would page on a single sample and a spike
        # during a backup would wake somebody every night.
        assert rule["min"] >= 1, rule


def test_existing_rules_are_never_overwritten():
    """A client who raised the CPU threshold because their server runs hot
    meant to. A converge that reset it nightly is worse than shipping no
    default at all."""
    posted = []

    def fake_req(method, url, *, token=None, body=None):
        if method == "GET" and "systems/records" in url:
            return {"items": [{"id": "sys1"}]}
        if method == "GET" and "alerts/records" in url:
            return {"items": [{"id": "a1", "name": "CPU", "value": 99}]}
        posted.append(body)
        return {}

    import unittest.mock as _mock
    with _mock.patch.object(seed, "_req", fake_req):
        state = seed.configure_alert_rules("http://h", "tok", "user1")
    names = [p["name"] for p in posted]
    assert "CPU" not in names, "the client's own CPU threshold was overwritten"
    assert "Status" in names, "the missing rules were not created"
    assert state.startswith("created")


def test_no_registered_system_is_reported_not_treated_as_success():
    """The first pass runs before the agent exists, so zero systems is the
    normal state there -- but it has to be distinguishable, because the second
    pass waits on exactly this string."""
    def fake_req(method, url, *, token=None, body=None):
        assert "systems/records" in url
        return {"items": []}

    import unittest.mock as _mock
    with _mock.patch.object(seed, "_req", fake_req):
        assert seed.configure_alert_rules("http://h", "tok", "u") == "no-systems"
