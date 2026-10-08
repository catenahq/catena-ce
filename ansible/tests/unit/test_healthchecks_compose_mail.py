"""Healthchecks sends through the outgoing mail chosen in Settings > Mail, and
has no mailer when mail is off.

healthchecks.compose.yml.j2 is rendered the way the converge renders it, with
infrastructure_smtp resolved by the real filter, and read back as YAML.

Run: uv run pytest tests/unit/test_healthchecks_compose_mail.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TEMPLATE = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "templates"
            / "healthchecks.compose.yml.j2")
PLUGIN = ANSIBLE / "playbooks" / "filter_plugins" / "smtp_resolve.py"

_spec = importlib.util.spec_from_file_location("smtp_resolve", PLUGIN)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
resolve = _mod.catena_smtp_resolve

PASSWORD = 're_ab"c$HOME'


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _environment(provider: str) -> dict:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = _bool
    env.filters["to_json"] = json.dumps
    rendered = env.from_string(TEMPLATE.read_text()).render(
        ansible_managed="",
        healthchecks_image="healthchecks/healthchecks:test",
        catena_public_surface_deferred=False,
        healthchecks_network_alias="healthchecks",
        healthchecks_internal_port=8000,
        healthchecks_host_localhost_port=18000,
        healthchecks_hostname="healthchecks.acme.test",
        cloudflare_zone="acme.test",
        healthchecks_secret_key="sk",
        healthchecks_superuser_password="su",
        healthchecks_ping_key="pk",
        admin_email="admin@acme.test",
        smtp_password=PASSWORD,
        infrastructure_smtp=resolve(provider, sender="no-reply@acme.test",
                                    fallback_sender="admin@acme.test"),
    )
    return yaml.safe_load(rendered)["services"]["app"]["environment"]


def test_mail_on_sends_through_the_chosen_relay():
    env = _environment("resend")
    assert env["EMAIL_HOST"] == "smtp.resend.com"
    assert env["EMAIL_PORT"] == "587"
    assert env["EMAIL_HOST_USER"] == "resend"
    assert env["EMAIL_USE_TLS"] == "True" and env["EMAIL_USE_SSL"] == "False"
    assert env["DEFAULT_FROM_EMAIL"] == "no-reply@acme.test"


def test_the_password_survives_yaml_and_stack_interpolation():
    """docker stack deploy reads `$$` as a literal `$`."""
    assert _environment("resend")["EMAIL_HOST_PASSWORD"] == PASSWORD.replace("$", "$$")


def test_mail_off_leaves_no_mailer_and_no_credential():
    env = _environment("none")
    assert env["EMAIL_HOST"] == ""
    for key in ("EMAIL_HOST_PASSWORD", "EMAIL_HOST_USER", "EMAIL_PORT"):
        assert key not in env, f"{key} is rendered with mail turned off"
    assert env["DEFAULT_FROM_EMAIL"] == "no-reply@acme.test"


def test_the_seed_learns_whether_mail_is_on():
    """The seed keeps the admin email channel only while there is a mailer to
    send it, so the task hands it the same switch the compose renders from."""
    tasks = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks"
             / "healthchecks.yml").read_text()
    assert "CATENA_MAIL_ENABLED={{ infrastructure_smtp.enabled | bool | lower }}" in tasks
