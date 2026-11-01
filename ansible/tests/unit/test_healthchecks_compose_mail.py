"""Healthchecks sends through the outgoing mail chosen in Settings > Mail, and
has no mailer when mail is off. The values the converge owns reach the app as
its .env (the env map of tasks/healthchecks.yml), written by `catena-apps
defaults`, so the service spec `docker service inspect` prints holds no
credential of this role's.

The env map is rendered the way the converge renders it, with
infrastructure_smtp resolved by the real filter.

Run: uv run pytest tests/unit/test_healthchecks_compose_mail.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INFRA = ANSIBLE / "reconcile" / "roles" / "infrastructure"
PLUGINS = ANSIBLE / "playbooks" / "filter_plugins"
TASKS = INFRA / "tasks" / "healthchecks.yml"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PLUGINS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


resolve = _load("smtp_resolve").catena_smtp_resolve

PASSWORD = 're_ab"c$HOME'


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _env_task() -> dict:
    return next(t for t in _tasks()
                if "_healthchecks_env" in (t.get("ansible.builtin.set_fact") or {}))


def _environment(provider: str, port: str = "", deferred: bool = False) -> dict:
    smtp = resolve(provider, host="mail.acme.test", port=port,
                   sender="no-reply@acme.test", fallback_sender="admin@acme.test")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = _bool
    values = dict(
        smtp_password=PASSWORD,
        infrastructure_smtp=smtp,
        catena_public_surface_deferred=deferred,
        healthchecks_network_alias="healthchecks",
        healthchecks_internal_port=8000,
        healthchecks_hostname="healthchecks.acme.test",
        cloudflare_zone="acme.test",
        inventory_hostname="host-a",
    )
    spec = _env_task()["ansible.builtin.set_fact"]["_healthchecks_env"]
    return {key: env.from_string(text).render(**values).strip()
            for key, text in spec.items()}


def test_mail_on_sends_through_the_chosen_relay():
    env = _environment("resend")
    assert env["EMAIL_HOST"] == "smtp.resend.com"
    assert env["EMAIL_PORT"] == "587"
    assert env["EMAIL_HOST_USER"] == "resend"
    assert env["EMAIL_USE_TLS"] == "True" and env["EMAIL_USE_SSL"] == "False"
    assert env["DEFAULT_FROM_EMAIL"] == "no-reply@acme.test"


def test_the_port_decides_the_connection_security():
    """465 is implicit TLS (Django's EMAIL_USE_SSL); the two are exclusive."""
    for port, tls, ssl in (("587", "True", "False"), ("2525", "True", "False"),
                           ("465", "False", "True")):
        env = _environment("server", port)
        assert env["EMAIL_PORT"] == port
        assert (env["EMAIL_USE_TLS"], env["EMAIL_USE_SSL"]) == (tls, ssl), port


def test_the_mail_password_is_the_apps_own_and_never_written_by_the_converge():
    """The block manages EMAIL_HOST_PASSWORD from the store's smtp_password, so
    the engine would refuse a request that writes it, and the password is in no
    request the converge sends."""
    for provider in ("resend", "none"):
        assert "EMAIL_HOST_PASSWORD" not in _environment(provider)
    assert "smtp_password" not in json.dumps(_env_task())


def test_mail_off_leaves_no_mailer_and_no_credential():
    env = _environment("none")
    assert env["EMAIL_HOST"] == ""
    for key in ("EMAIL_HOST_USER", "EMAIL_PORT", "EMAIL_USE_TLS", "EMAIL_USE_SSL"):
        assert env[key] == "", f"{key} is set with mail turned off"
    assert env["DEFAULT_FROM_EMAIL"] == "no-reply@acme.test"


def test_the_env_map_never_names_the_seeds_credentials():
    """The seed's values (superuser password, ping key, API keys) reach the
    container on the seed's stdin, not in the app's .env."""
    env = _environment("resend")
    for key in ("SUPERUSER_PASSWORD", "PING_KEY", "API_KEY_READONLY", "API_KEY_READWRITE"):
        assert key not in env
    assert "SECRET_KEY" not in env, "the app generates its own secret key"


def test_the_address_follows_the_domain():
    env = _environment("resend")
    assert env["SITE_ROOT"] == "https://healthchecks.acme.test"
    assert env["SITE_NAME"] == "acme.test heartbeats"
    deferred = _environment("resend", deferred=True)
    assert deferred["SITE_ROOT"] == "http://healthchecks:8000"
    assert deferred["SITE_NAME"] == "heartbeats"


def test_the_loopback_ip_is_an_allowed_host_in_both_addresses():
    """Every host-side consumer dials the loopback publish by IP; Django answers
    400 DisallowedHost to a Host it does not list."""
    for deferred in (False, True):
        hosts = _environment("resend", deferred=deferred)["ALLOWED_HOSTS"].split(",")
        assert "127.0.0.1" in hosts and "healthchecks" in hosts, hosts
    assert "healthchecks.acme.test" in _environment("resend")["ALLOWED_HOSTS"].split(",")


def test_the_env_task_hides_its_values():
    assert _env_task()["no_log"] is True


def test_the_converge_holds_no_swarm_secret_for_the_app():
    text = TASKS.read_text()
    assert "swarm_secret" not in text
    assert "swarm_stack.yml" not in text


def test_the_values_are_written_before_the_app_is_added():
    names = [t.get("name", "") for t in _tasks()]
    assert names.index("Healthchecks: the values the converge owns") < names.index(
        "Healthchecks: add the app")


def test_the_project_is_named_after_the_host():
    """The app's configure hook keeps the project it names."""
    assert _environment("resend")["HC_PROJECT_NAME"] == "host-a"


def test_the_converge_says_so_when_mail_is_off():
    """A host whose alerts reach no mailbox must not look like a healthy one."""
    warn = next(t for t in _tasks() if t.get("name") == "Healthchecks: warn when outgoing mail is off")
    assert warn["when"] == "not (infrastructure_smtp.enabled | bool)"
