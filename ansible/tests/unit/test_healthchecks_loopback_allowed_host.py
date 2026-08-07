"""Every host-side consumer dials Healthchecks by loopback IP, so the IP has
to be in ALLOWED_HOSTS.

Django matches ALLOWED_HOSTS against the literal Host header. "localhost" does
not cover a request to http://127.0.0.1:18000 -- verified against
healthchecks/healthchecks:v4.3: Host 127.0.0.1 returned 400 DisallowedHost,
Host localhost returned 401 (reached the app). gatus-sync, the clamav watchdog
and the mail canary all use the IP form, so all three were failing in the shape
that is hardest to notice: a monitor reporting nothing.

Run: uv run pytest tests/unit/test_healthchecks_loopback_allowed_host.py
"""
from __future__ import annotations

import re
from pathlib import Path

INFRA = (
    Path(__file__).resolve().parents[2]
    / "roles" / "infrastructure"
)
COMPOSE = INFRA / "templates" / "healthchecks.compose.yml.j2"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

# Files that build a Healthchecks URL from the loopback publish.
LOOPBACK_CONSUMERS = [
    INFRA / "templates" / "gatus-sync.env.j2",
    SCRIPTS / "run-clamav-watch.sh",
    SCRIPTS / "run-mail-canary.sh",
]


def _allowed_hosts() -> list[str]:
    for line in COMPOSE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ALLOWED_HOSTS:"):
            value = stripped.split(":", 1)[1].strip().strip('"')
            return [h.strip() for h in value.split(",")]
    raise AssertionError("healthchecks.compose.yml.j2 defines no ALLOWED_HOSTS")


def test_the_loopback_ip_is_an_allowed_host():
    hosts = _allowed_hosts()
    assert "127.0.0.1" in hosts, (
        "host-side units reach Healthchecks at http://127.0.0.1:<port>; "
        "Django answers 400 DisallowedHost unless the IP itself is listed. "
        f"ALLOWED_HOSTS is {hosts}"
    )


def test_the_container_alias_and_public_hostname_are_still_allowed():
    """The loopback entry is an addition, not a replacement: Gatus posts to the
    network alias and the client reaches the UI on the public hostname."""
    hosts = _allowed_hosts()
    assert "{{ healthchecks_network_alias }}" in hosts
    assert "{{ healthchecks_hostname }}" in hosts


def test_every_loopback_consumer_uses_a_host_the_container_admits():
    """Catches the next consumer added with a host form nobody allowed."""
    hosts = set(_allowed_hosts())
    pattern = re.compile(r"https?://([A-Za-z0-9_.-]+):\{\{\s*healthchecks_host_localhost_port")
    seen = 0
    for path in LOOPBACK_CONSUMERS:
        text = path.read_text()
        for host in pattern.findall(text):
            seen += 1
            assert host in hosts, (
                f"{path.name} dials Healthchecks as {host!r}, which is not in "
                f"ALLOWED_HOSTS ({sorted(hosts)})"
            )
    # The shell watchdogs interpolate the port from their env file rather than
    # from Jinja, so match their form too.
    shell_pattern = re.compile(r"https?://([A-Za-z0-9_.-]+):\$\{HEALTHCHECKS_LOOPBACK_PORT")
    for path in LOOPBACK_CONSUMERS:
        for host in shell_pattern.findall(path.read_text()):
            seen += 1
            assert host in hosts, (
                f"{path.name} dials Healthchecks as {host!r}, which is not in "
                f"ALLOWED_HOSTS ({sorted(hosts)})"
            )
    assert seen >= 3, (
        "expected to find the gatus-sync, clamav-watch and mail-canary "
        f"loopback URLs; found {seen}"
    )
