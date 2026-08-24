"""Where this server sends mail from is ONE choice, not a precedence rule.

It used to be inferred from which of three sender-address keys was non-empty --
RESEND_SENDER_EMAIL, BREVO_SENDER_EMAIL, SMTP_HOST -- with Resend winning when
two were filled. Three defects in one shape:

  - the answer to "where does mail go" was spread across three fields plus a
    tie-break that appeared on no page the client could read;
  - switching provider without first CLEARING the old address kept sending
    through the old one, silently and forever;
  - the same ladder was written out twice, in roles/keycloak/defaults and in
    roles/infrastructure wordpress_plugins.yml, free to drift.

SMTP_PROVIDER (resend | brevo | server) decides, SMTP_SENDER is the address,
smtp_password is the one credential. The two known relays supply their own host,
port and -- for Resend, whose SMTP username is the literal string `resend` for
every account -- their username.

The expressions live in a role's defaults, so this renders them with plain
Jinja2: every filter they use (trim, lower, default, int) is stock Jinja, not an
Ansible extension.

Run: uv run pytest tests/unit/test_smtp_resolution.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ANSIBLE = Path(__file__).resolve().parents[2]
KEYCLOAK_DEFAULTS = ANSIBLE / "roles" / "keycloak" / "defaults" / "main.yml"
WORDPRESS = ANSIBLE / "roles" / "infrastructure" / "tasks" / "wordpress_plugins.yml"
ONBOX_CONFIG = ANSIBLE / "helpers" / "onbox_config.py"

# Resolved in dependency order: _smtp_provider first, then everything that
# reads it.
_ORDER = ("_smtp_provider", "smtp_enabled", "smtp_host", "smtp_port",
          "smtp_user", "smtp_from")


def _resolve(**store) -> dict:
    """Render the role's smtp_* defaults against a store's cfg_* values."""
    raw = yaml.safe_load(KEYCLOAK_DEFAULTS.read_text())
    env = Environment()
    ctx = {"admin_email": "admin@example.test"}
    ctx.update(store)
    out = {}
    for name in _ORDER:
        assert name in raw, f"{name} is gone from roles/keycloak/defaults"
        value = env.from_string(str(raw[name])).render(**ctx).strip()
        ctx[name] = value
        out[name] = value
    return out


def test_resend_supplies_its_own_host_port_and_username():
    got = _resolve(cfg_smtp_provider="resend", cfg_smtp_sender="no-reply@acme.test")
    assert got["smtp_enabled"] == "True"
    assert got["smtp_host"] == "smtp.resend.com"
    assert got["smtp_port"] == "587"
    # Every Resend account authenticates as the literal string `resend`.
    assert got["smtp_user"] == "resend"
    assert got["smtp_from"] == "no-reply@acme.test"


def test_brevo_supplies_host_and_port_but_the_login_is_account_specific():
    got = _resolve(
        cfg_smtp_provider="brevo",
        cfg_smtp_sender="no-reply@acme.test",
        cfg_smtp_user="8a1b2c@smtp-brevo.com",
    )
    assert got["smtp_host"] == "smtp-relay.brevo.com"
    assert got["smtp_port"] == "587"
    assert got["smtp_user"] == "8a1b2c@smtp-brevo.com"


def test_a_self_hosted_relay_uses_every_field_as_given():
    got = _resolve(
        cfg_smtp_provider="server",
        cfg_smtp_sender="no-reply@acme.test",
        cfg_smtp_host="mail.acme.test",
        cfg_smtp_port="2525",
        cfg_smtp_user="relay-user",
    )
    assert got["smtp_host"] == "mail.acme.test"
    assert got["smtp_port"] == "2525"
    assert got["smtp_user"] == "relay-user"


def test_no_provider_means_no_smtp_rather_than_a_guess():
    """A realm with no mail configured says so on its own login page. Mail that
    is configured, accepted and silently goes nowhere does not."""
    for provider in ("", "   ", "sendgrid", "RESEND-ISH"):
        got = _resolve(cfg_smtp_provider=provider, cfg_smtp_sender="x@acme.test")
        assert got["smtp_enabled"] == "False", provider
        assert got["smtp_host"] == "", provider


def test_the_provider_is_read_case_and_whitespace_insensitively():
    got = _resolve(cfg_smtp_provider="  Resend ", cfg_smtp_sender="x@acme.test")
    assert got["smtp_host"] == "smtp.resend.com"


def test_the_sender_falls_back_to_the_admin_address():
    # Not to an empty From: a message with no envelope sender is refused by
    # most relays, which turns a missing field into an unexplained bounce.
    got = _resolve(cfg_smtp_provider="resend")
    assert got["smtp_from"] == "admin@example.test"


def test_switching_provider_cannot_leave_the_old_one_in_use():
    """The defect the choice replaces. Under the old rule, filling the Brevo
    address while a Resend address was still stored kept sending through
    Resend -- the client had to know to clear a field to change a setting."""
    got = _resolve(
        cfg_smtp_provider="brevo",
        cfg_smtp_sender="no-reply@acme.test",
        cfg_smtp_user="8a1b2c@smtp-brevo.com",
        # Stale values from an earlier configuration, still in the store.
        cfg_smtp_host="mail.old-relay.test",
    )
    assert got["smtp_host"] == "smtp-relay.brevo.com"


def test_implicit_tls_is_not_offered_and_not_stored():
    """SMTP_USE_TLS was collected, validated and stored, and read by nothing:
    realm-vps.yaml.j2 renders starttls true and ssl false as literals. A key
    that accepts a value and changes nothing answers "is TLS configured" with a
    yes."""
    raw = yaml.safe_load(KEYCLOAK_DEFAULTS.read_text())
    assert "smtp_use_tls" not in raw, (
        "smtp_use_tls is back in the role defaults; the realm template does not "
        "read it")
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
    """Both copies of the resolution, and the store registry. A reader left
    behind reads a key nothing writes any more, which resolves to empty and
    takes its branch of the ladder with it."""
    for path in (KEYCLOAK_DEFAULTS, WORDPRESS, ONBOX_CONFIG):
        body = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert gone not in body, f"{gone} is still read in {path.name}"
