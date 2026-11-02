"""Every host-side consumer dials Healthchecks by loopback IP, so the IP has
to be in ALLOWED_HOSTS.

Django matches ALLOWED_HOSTS against the literal Host header. "localhost" does
not cover a request to http://127.0.0.1:18000: Host 127.0.0.1 answers 400
DisallowedHost unless the IP is listed, while Host localhost reaches the app. A consumer on the IP form fails
in the shape that is hardest to notice: a monitor reporting nothing.

The ALLOWED_HOSTS the converge writes into the app's .env is
tasks/healthchecks.yml's env map.

Run: uv run pytest tests/unit/test_healthchecks_loopback_allowed_host.py
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

INFRA = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "infrastructure"
)

# Files that build a Healthchecks URL from the loopback publish.
LOOPBACK_CONSUMERS = [
    INFRA / "templates" / "gatus-sync.env.j2",
]


def _allowed_hosts(deferred: bool) -> list[str]:
    """The ALLOWED_HOSTS the converge writes, for a host with and without a domain."""
    tasks = yaml.safe_load((INFRA / "tasks" / "healthchecks.yml").read_text())
    spec = next(t["ansible.builtin.set_fact"]["_healthchecks_env"] for t in tasks
                if "_healthchecks_env" in (t.get("ansible.builtin.set_fact") or {}))
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes", "on")
    value = env.from_string(spec["ALLOWED_HOSTS"]).render(
            catena_public_surface_deferred=deferred,
            healthchecks_network_alias="healthchecks",
            healthchecks_hostname="healthchecks.acme.test")
    return [h.strip() for h in value.strip().split(",")]


def test_every_loopback_consumer_uses_a_host_the_container_admits():
    """Catches the next consumer added with a host form nobody allowed."""
    pattern = re.compile(r"https?://([A-Za-z0-9_.-]+):\{\{\s*healthchecks_host_localhost_port")
    seen = 0
    for deferred in (False, True):
        hosts = set(_allowed_hosts(deferred))
        for path in LOOPBACK_CONSUMERS:
            for host in pattern.findall(path.read_text()):
                seen += 1
                assert host in hosts, (
                    f"{path.name} dials Healthchecks as {host!r}, which is not in "
                    f"ALLOWED_HOSTS ({sorted(hosts)})"
                )
    assert seen >= 2, (
        f"expected to find the gatus-sync loopback URL in both branches; found {seen}"
    )
