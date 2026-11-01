"""Where this server sends mail from is ONE choice, resolved in ONE place.

SMTP_PROVIDER (resend | brevo | server) decides, SMTP_SENDER is the address,
smtp_password is the one credential, and the ladder that reads them lives in the
filter plugin this file exercises.

Inferring the provider instead from which of three sender-address keys is
non-empty -- RESEND_SENDER_EMAIL, BREVO_SENDER_EMAIL, SMTP_HOST -- with Resend
winning when two are filled, carries three defects in one shape:

  - the answer to "where does mail go" is spread across three fields plus a
    tie-break that appears on no page the client can read;
  - switching provider without first CLEARING the old address keeps sending
    through the old one, silently, for as long as the stale value sits there;
  - the ladder is written out once per consumer -- keycloak defaults, Beszel,
    Healthchecks -- each free to drift, and a host whose password resets and
    whose alerts go through different relays, from the same stored config,
    reports nothing.

Run: uv run pytest tests/unit/test_smtp_resolution.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
PLUGIN = ANSIBLE / "playbooks" / "filter_plugins" / "smtp_resolve.py"
KEYCLOAK_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "keycloak" / "defaults" / "main.yml"
BESZEL = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks" / "beszel.yml"
INFRA_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "defaults" / "main.yml"
HEALTHCHECKS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks" / "healthchecks.yml"
ONBOX_CONFIG = ANSIBLE / "helpers" / "onbox_config.py"


def _resolve():
    spec = importlib.util.spec_from_file_location("smtp_resolve", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.catena_smtp_resolve


def test_resend_supplies_its_own_host_port_and_username():
    got = _resolve()("resend", sender="no-reply@acme.test")
    assert got["enabled"] is True
    assert got["host"] == "smtp.resend.com"
    assert got["port"] == 587
    # Every Resend account authenticates as the literal string `resend`.
    assert got["user"] == "resend"
    assert got["sender"] == "no-reply@acme.test"


def test_brevo_supplies_host_and_port_but_the_login_is_account_specific():
    got = _resolve()("brevo", sender="no-reply@acme.test",
                     user="8a1b2c@smtp-brevo.com")
    assert got["host"] == "smtp-relay.brevo.com"
    assert got["port"] == 587
    assert got["user"] == "8a1b2c@smtp-brevo.com"


def test_a_self_hosted_relay_uses_every_field_as_given():
    got = _resolve()("server", host="mail.acme.test", port="2525",
                     user="relay-user", sender="no-reply@acme.test")
    assert got["host"] == "mail.acme.test"
    assert got["port"] == 2525
    assert got["user"] == "relay-user"


def test_a_relay_with_no_port_falls_back_to_submission_not_25():
    """Port 25 is blocked outbound by most providers, and the failure looks
    like a credential problem rather than a blocked port."""
    for blank in ("", "   ", None, "not-a-number"):
        assert _resolve()("server", host="mail.acme.test", port=blank)["port"] == 587


def test_no_provider_means_no_smtp_rather_than_a_guess():
    """A service with no mail configured says so. Mail that is configured,
    accepted and silently goes nowhere does not."""
    for provider in ("", "   ", "none", "sendgrid", "RESEND-ISH", None):
        got = _resolve()(provider, sender="x@acme.test")
        assert got["enabled"] is False, provider
        assert got["host"] == "", provider


def test_the_provider_is_read_case_and_whitespace_insensitively():
    assert _resolve()("  Resend ", sender="x@acme.test")["host"] == "smtp.resend.com"


def test_the_sender_falls_back_to_the_admin_address():
    # Not to an empty From: a message with no envelope sender is refused by
    # most relays, which turns a missing field into an unexplained bounce.
    got = _resolve()("resend", fallback_sender="admin@example.test")
    assert got["sender"] == "admin@example.test"


def test_switching_provider_cannot_leave_the_old_one_in_use():
    """The provider is an explicit choice, so a stale value from an earlier
    configuration changes nothing. Inferred from which address is filled, a
    stored Resend address would keep mail on Resend after the client fills
    the Brevo one -- the client would have to know to clear a field to change
    a setting."""
    got = _resolve()("brevo", sender="no-reply@acme.test",
                     user="8a1b2c@smtp-brevo.com",
                     # Stale values from an earlier configuration.
                     host="mail.old-relay.test", port="2525")
    assert got["host"] == "smtp-relay.brevo.com"
    assert got["port"] == 587


# ─── one resolution, every consumer ────────────────────────────────────────


def _code(path: Path) -> str:
    return "\n".join(
        ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("path", [KEYCLOAK_DEFAULTS, INFRA_DEFAULTS])
def test_every_consumer_calls_the_filter_rather_than_rewriting_it(path):
    """A hand-written copy of the ladder is free to drift, and the symptom is
    a host that sends password-reset mail through one relay and its alerts
    through another, from the same stored config, with nothing
    reporting it."""
    assert "catena_smtp_resolve" in path.read_text(), (
        f"{path.name} does not use the shared resolution")
    # The giveaway of a hand-rolled copy: naming a provider's hostname.
    for literal in ("smtp.resend.com", "smtp-relay.brevo.com"):
        assert literal not in _code(path), (
            f"{path.name} hardcodes {literal}; that belongs to the filter")


def test_beszel_is_given_no_mail_setting_by_the_converge():
    """Beszel's app reads the mail choice and the mail password from the store
    itself (the wiring contract lists smtp_password among its secrets), so the
    converge writes none into its .env."""
    code = _code(BESZEL)
    assert "infrastructure_smtp" not in code and "smtp_password" not in code
    assert "EMAIL_" not in code and "SMTP_" not in code


@pytest.mark.parametrize("path", [HEALTHCHECKS])
def test_the_infrastructure_consumers_read_its_resolution(path):
    """Healthchecks's alert mail takes the infrastructure role's one resolution
    rather than a copy of it."""
    code = _code(path)
    assert "infrastructure_smtp." in code, (
        f"{path.name} does not read infrastructure_smtp")
    assert "catena_smtp_resolve" not in code, (
        f"{path.name} resolves the mail choice a second time")
    for literal in ("smtp.resend.com", "smtp-relay.brevo.com"):
        assert literal not in code, (
            f"{path.name} hardcodes {literal}; that belongs to the filter")


def test_the_infrastructure_resolution_sends_from_the_admin_email_when_no_sender_is_set():
    """The sender the infrastructure role resolves falls back to the admin
    email, as for every other sender on the host."""
    import jinja2
    import yaml

    expr = yaml.safe_load(INFRA_DEFAULTS.read_text())["infrastructure_smtp"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["catena_smtp_resolve"] = _resolve()
    body = expr.strip().removeprefix("{{").removesuffix("}}")
    smtp = env.from_string(f"{{% set r = {body} %}}{{{{ r.sender }}}}").render(
        cfg_smtp_provider="resend", cfg_smtp_host="", cfg_smtp_port="",
        cfg_smtp_user="", cfg_smtp_sender="", admin_email="admin@acme.test")
    assert smtp == "admin@acme.test"


@pytest.mark.parametrize("port,implicit", [
    ("465", True), ("587", False), ("2525", False), ("", False), ("25", False)])
def test_the_port_decides_implicit_tls(port, implicit):
    got = _resolve()("server", host="mail.acme.test", port=port)
    assert got["implicit_tls"] is implicit


def test_the_known_relays_and_no_mail_are_not_implicit_tls():
    for provider in ("resend", "brevo", "none"):
        assert _resolve()(provider)["implicit_tls"] is False, provider


def test_the_implicit_tls_port_is_465():
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location("smtp_resolve", PLUGIN))
    module.__spec__.loader.exec_module(module)
    assert module._IMPLICIT_TLS_PORT == 465


def _keycloak_realm_smtp(port: str) -> dict:
    """keycloak_smtp_server as the role's defaults render it for a custom
    server on `port`."""
    import ast

    import jinja2
    import yaml

    smtp = _resolve()("server", host="mail.acme.test", port=port, user="u",
                      sender="no-reply@acme.test")
    expr = yaml.safe_load(KEYCLOAK_DEFAULTS.read_text())["keycloak_smtp_server"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: str(v).lower() in ("1", "true", "yes", "on")
    text = env.from_string(expr).render(
        _smtp=smtp, smtp_enabled=smtp["enabled"], smtp_host=smtp["host"],
        smtp_port=smtp["port"], smtp_user=smtp["user"], smtp_from=smtp["sender"],
        keycloak_realm_display_name="Acme")
    return ast.literal_eval(text)


@pytest.mark.parametrize("port,starttls,ssl", [
    ("465", "false", "true"), ("587", "true", "false"), ("2525", "true", "false")])
def test_the_realm_mail_settings_follow_the_port(port, starttls, ssl):
    realm = _keycloak_realm_smtp(port)
    assert realm["port"] == port
    assert (realm["starttls"], realm["ssl"]) == (starttls, ssl)


def test_no_tls_setting_is_stored():
    """The port is the only input: an SMTP_USE_TLS key would be collected,
    stored and read by nothing."""
    assert "SMTP_USE_TLS" not in ONBOX_CONFIG.read_text(), (
        "SMTP_USE_TLS is back in the store registry")
    assert "smtp_use_starttls" not in KEYCLOAK_DEFAULTS.read_text()


@pytest.mark.parametrize("gone", [
    "cfg_resend_sender_email", "cfg_brevo_sender_email", "cfg_brevo_smtp_user",
    "cfg_smtp_from", "cfg_smtp_use_tls",
])
def test_no_consumer_still_reads_a_retired_key(gone):
    """A reader of a key nothing writes resolves it to empty and takes its
    branch of the ladder with it."""
    for path in (KEYCLOAK_DEFAULTS, BESZEL, INFRA_DEFAULTS,
                 HEALTHCHECKS, ONBOX_CONFIG):
        body = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert gone not in body, f"{gone} is still read in {path.name}"
