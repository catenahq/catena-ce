"""Both update gates read Gatus's endpoint statuses on the host, at the port
Gatus is published on.

The daily chain's pre-update gate (DAILY_GATUS_API_URL in daily.env) and the
health baseline the update lanes compare a bump against
(AUTO_UPDATE_GATUS_API_URL in stack-update.env) each leave Gatus out while
their key is blank. Both run as units on the host, so the address is Gatus's
loopback publish, and both take it from the one variable that builds it from
the port reconcile/roles/infrastructure publishes.

Run: uv run pytest tests/unit/test_update_gates_read_gatus_on_the_host.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
ADMIN_TEMPLATES = ROLES / "catena-admin" / "templates"

# Each env template, and the key its reader takes the statuses from.
READERS = (
    ("daily.env.j2", "DAILY_GATUS_API_URL"),
    ("stack-update.env.j2", "AUTO_UPDATE_GATUS_API_URL"),
)


def _defaults(role: str) -> dict:
    return yaml.safe_load((ROLES / role / "defaults" / "main.yml").read_text())


def _render(text: str, **context) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(text).render(**context)


def _rendered_line(template: str, key: str) -> str:
    """The template's line for key, rendered from the roles' defaults."""
    lines = [line for line in (ADMIN_TEMPLATES / template).read_text().splitlines()
             if line.startswith(f"{key}=")]
    assert len(lines) == 1, f"{template} sets {key} {len(lines)} times"
    url = _render(_defaults("catena-admin")["catena_gatus_statuses_url"],
                  gatus_host_localhost_port=_defaults("infrastructure")["gatus_host_localhost_port"])
    return _render(lines[0], catena_gatus_statuses_url=url)


def test_each_gate_reads_the_statuses_on_the_loopback_publish():
    port = _defaults("infrastructure")["gatus_host_localhost_port"]
    for template, key in READERS:
        assert _rendered_line(template, key) == (
            f"{key}=http://127.0.0.1:{port}/api/v1/endpoints/statuses")


def test_the_url_is_built_from_the_port_gatus_is_published_on():
    """A second literal port would go on pointing at the old one when the
    publish moves, and the gates read an unreachable Gatus as no opinion."""
    compose = (ROLES / "infrastructure" / "templates" / "gatus.compose.yml.j2").read_text()
    assert "published: {{ gatus_host_localhost_port }}" in compose
    assert "{{ gatus_host_localhost_port }}" in _defaults("catena-admin")["catena_gatus_statuses_url"]
