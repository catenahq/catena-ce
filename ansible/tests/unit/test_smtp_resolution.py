"""Where this server sends mail from is ONE choice, resolved in ONE place.

It used to be inferred from which of three sender-address keys was non-empty --
RESEND_SENDER_EMAIL, BREVO_SENDER_EMAIL, SMTP_HOST -- with Resend winning when
two were filled. Three defects in one shape:

  - the answer to "where does mail go" was spread across three fields plus a
    tie-break that appeared on no page the client could read;
  - switching provider without first CLEARING the old address kept sending
    through the old one, silently and for as long as the stale value sat there;
  - the same ladder was written out twice, in roles/keycloak/defaults and in
    roles/infrastructure wordpress_plugins.yml, free to drift from each other.

SMTP_PROVIDER (resend | brevo | server) decides, SMTP_SENDER is the address,
smtp_password is the one credential. The ladder itself now lives in the filter
plugin this file exercises, because the third copy was about to be written for
Beszel -- and a host whose password resets and whose contact form go through
different relays, from the same stored config, reports nothing.

Run: uv run pytest tests/unit/test_smtp_resolution.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
PLUGIN = ANSIBLE / "playbooks" / "filter_plugins" / "smtp_resolve.py"
KEYCLOAK_DEFAULTS = ANSIBLE / "roles" / "keycloak" / "defaults" / "main.yml"
WORDPRESS = ANSIBLE / "roles" / "infrastructure" / "tasks" / "wordpress_plugins.yml"
BESZEL = ANSIBLE / "roles" / "infrastructure" / "tasks" / "beszel.yml"
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
    """The defect the choice replaces. Under the old rule, filling the Brevo
    address while a Resend address was still stored kept sending through
    Resend -- the client had to know to clear a field to change a setting."""
    got = _resolve()("brevo", sender="no-reply@acme.test",
                     user="8a1b2c@smtp-brevo.com",
                     # Stale values from an earlier configuration.
                     host="mail.old-relay.test", port="2525")
    assert got["host"] == "smtp-relay.brevo.com"
    assert got["port"] == 587


# ─── one resolution, three consumers ───────────────────────────────────────


@pytest.mark.parametrize("path", [KEYCLOAK_DEFAULTS, WORDPRESS, BESZEL])
def test_every_consumer_calls_the_filter_rather_than_rewriting_it(path):
    """The ladder was written out twice and was about to be a third time. Each
    copy is free to drift, and the symptom is a host that sends password-reset
    mail through one relay and a contact form through another, from the same
    stored config, with nothing reporting it."""
    body = path.read_text()
    assert "catena_smtp_resolve" in body, (
        f"{path.name} does not use the shared resolution")
    # The giveaway of a hand-rolled copy: naming a provider's hostname.
    for literal in ("smtp.resend.com", "smtp-relay.brevo.com"):
        code = "\n".join(
            ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        assert literal not in code, (
            f"{path.name} hardcodes {literal}; that belongs to the filter")


def test_implicit_tls_is_not_offered_and_not_stored():
    """SMTP_USE_TLS was collected, validated and stored, and read by nothing:
    realm-vps.yaml.j2 renders starttls true and ssl false as literals. A key
    that accepts a value and changes nothing answers "is TLS configured" with a
    yes."""
    assert "SMTP_USE_TLS" not in ONBOX_CONFIG.read_text(), (
        "SMTP_USE_TLS is back in the store registry")
    realm = (ANSIBLE / "roles" / "keycloak" / "templates"
             / "realm-vps.yaml.j2").read_text()
    assert 'ssl: "false"' in realm, (
        "the realm no longer pins implicit TLS off; if that became configurable, "
        "this file's reasoning about the deleted key no longer holds")


@pytest.mark.parametrize("gone", [
    "cfg_resend_sender_email", "cfg_brevo_sender_email", "cfg_brevo_smtp_user",
    "cfg_smtp_from", "cfg_smtp_use_tls",
])
def test_no_consumer_still_reads_a_retired_key(gone):
    """A reader left behind reads a key nothing writes any more, which resolves
    to empty and takes its branch of the ladder with it."""
    for path in (KEYCLOAK_DEFAULTS, WORDPRESS, BESZEL, ONBOX_CONFIG):
        body = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert gone not in body, f"{gone} is still read in {path.name}"
