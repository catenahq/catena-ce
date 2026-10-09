"""Healthchecks sends through the outgoing mail chosen in Settings > Mail, and
has no mailer when mail is off. Its credentials are swarm secrets, so the
service environment `docker service inspect` prints holds none.

healthchecks.compose.yml.j2 is rendered the way the converge renders it, with
infrastructure_smtp resolved by the real filter and the secrets named by the
real swarm_secret_entries, and read back as YAML.

Run: uv run pytest tests/unit/test_healthchecks_compose_mail.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"
TEMPLATE = INFRA / "templates" / "healthchecks.compose.yml.j2"
PLUGINS = ANSIBLE / "playbooks" / "filter_plugins"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PLUGINS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


resolve = _load("smtp_resolve").catena_smtp_resolve
secret_entries = _load("catena_admin_service").secret_entries

PASSWORD = 're_ab"c$HOME'
SECRET_KEY = "hc-secret-key-value"


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _jinja() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = _bool
    return env


def _render(provider: str) -> tuple[str, dict]:
    smtp = resolve(provider, sender="no-reply@acme.test",
                   fallback_sender="admin@acme.test")
    specs = yaml.safe_load((INFRA / "defaults" / "main.yml").read_text())[
        "healthchecks_swarm_secrets"]
    secrets = secret_entries([
        {**s, "value": _jinja().from_string(s["value"]).render(
            healthchecks_secret_key=SECRET_KEY, smtp_password=PASSWORD,
            infrastructure_smtp=smtp)}
        for s in specs
    ])
    text = _jinja().from_string(TEMPLATE.read_text()).render(
        ansible_managed="",
        healthchecks_image="healthchecks/healthchecks:test",
        catena_public_surface_deferred=False,
        healthchecks_network="catena-healthchecks",
        healthchecks_network_alias="healthchecks",
        healthchecks_internal_port=8000,
        healthchecks_host_localhost_port=18000,
        healthchecks_hostname="healthchecks.acme.test",
        cloudflare_zone="acme.test",
        infrastructure_smtp=smtp,
        _healthchecks_secrets=secrets,
    )
    return text, yaml.safe_load(text)


def _environment(provider: str) -> dict:
    return _render(provider)[1]["services"]["app"]["environment"]


def test_mail_on_sends_through_the_chosen_relay():
    env = _environment("resend")
    assert env["EMAIL_HOST"] == "smtp.resend.com"
    assert env["EMAIL_PORT"] == "587"
    assert env["EMAIL_HOST_USER"] == "resend"
    assert env["EMAIL_USE_TLS"] == "True" and env["EMAIL_USE_SSL"] == "False"
    assert env["DEFAULT_FROM_EMAIL"] == "no-reply@acme.test"


def test_no_credential_is_in_the_service_environment():
    text, doc = _render("resend")
    assert PASSWORD not in text and SECRET_KEY not in text
    env = doc["services"]["app"]["environment"]
    for key in ("SECRET_KEY", "EMAIL_HOST_PASSWORD", "SUPERUSER_PASSWORD", "PING_KEY"):
        assert key not in env, f"{key} is in the service environment"


def test_each_credential_is_a_mounted_secret_healthchecks_reads():
    """hc/settings.py (v4.4) reads <NAME>_FILE through envsecret."""
    doc = _render("resend")[1]
    app = doc["services"]["app"]
    mounted = {s["target"]: s["source"] for s in app["secrets"]}
    assert set(mounted) == {"SECRET_KEY", "EMAIL_HOST_PASSWORD"}
    for target, source in mounted.items():
        assert app["environment"][f"{target}_FILE"] == f"/run/secrets/{target}"
        assert doc["secrets"][source] == {"external": True}


def test_mail_off_leaves_no_mailer_and_no_credential():
    doc = _render("none")[1]
    env = doc["services"]["app"]["environment"]
    assert env["EMAIL_HOST"] == ""
    for key in ("EMAIL_HOST_PASSWORD_FILE", "EMAIL_HOST_USER", "EMAIL_PORT"):
        assert key not in env, f"{key} is rendered with mail turned off"
    assert [s["target"] for s in doc["services"]["app"]["secrets"]] == ["SECRET_KEY"]
    assert env["DEFAULT_FROM_EMAIL"] == "no-reply@acme.test"


def test_a_new_mail_password_is_a_new_secret():
    """Swarm secrets are immutable, so the service follows a rotation only
    through a new name."""
    one = secret_entries([{"base": "healthchecks_email_password",
                           "target": "EMAIL_HOST_PASSWORD", "value": "a"}])
    two = secret_entries([{"base": "healthchecks_email_password",
                           "target": "EMAIL_HOST_PASSWORD", "value": "b"}])
    assert one[0]["name"] != two[0]["name"]


def test_the_converge_creates_the_secrets_before_the_deploy():
    tasks = yaml.safe_load((INFRA / "tasks" / "healthchecks.yml").read_text())
    names = [t.get("name", "") for t in tasks]
    create = names.index("Healthchecks: create the missing swarm secrets")
    assert create < names.index("Healthchecks: deploy as a swarm stack")
    assert tasks[create]["ansible.builtin.include_tasks"].endswith(
        "/tasks/swarm_secrets.yml")


def test_the_seed_learns_whether_mail_is_on():
    """The seed keeps the admin email channel only while there is a mailer to
    send it, so the task hands it the same switch the compose renders from."""
    tasks = (INFRA / "tasks" / "healthchecks.yml").read_text()
    assert "CATENA_MAIL_ENABLED={{ infrastructure_smtp.enabled | bool | lower }}" in tasks
