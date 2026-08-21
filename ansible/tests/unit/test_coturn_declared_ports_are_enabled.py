"""Every port coturn's registry fragment opens must be one coturn binds.

The public-port fragment in roles/coturn/tasks/deploy.yml declares udp on
coturn_tls_port ("TLS-TURN (UDP)"), the reconciler opens it in ufw, and
roles/coturn/tasks/validate.yml asserts the listener is there. Nothing tied
any of that to the config that has to ask for it.

On bench 1216 backup_rollback the converge failed on that assertion after the
image pin moved to 4.17.2 (catena-admin payload/engines/tier1/catalog.go,
5aa2c79): DTLS client listeners are opt-in there, so tls-listening-port bound
TCP only and the declared UDP port had nothing behind it. The same bump made
`no-loopback-peers` a "Bad configuration format" line and `no-cli` a
deprecation error. A pin can move in a sibling repo without this repo's config
being read again, so the pairing is asserted here instead.

Run: uv run pytest tests/unit/test_coturn_declared_ports_are_enabled.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COTURN = ANSIBLE / "roles" / "coturn"

_FRAGMENT_TASK = "coturn: declare public ports (registry fragment)"

# Directives the pinned coturn rejects outright. Each is the OPPOSITE of a
# default that flipped: loopback peers and the telnet CLI are both refused
# unless opted into (--allow-loopback-peers, --cli), and --no-dtls was
# superseded by DTLS simply not starting unless --dtls is given. Writing any
# of them back costs a converge, because coturn logs and continues rather
# than refusing to start.
_REJECTED = ("no-loopback-peers", "no-cli", "no-dtls")


def _config() -> str:
    return (COTURN / "templates" / "turnserver.conf.j2").read_text(
        encoding="utf-8"
    )


def _directives() -> set[str]:
    """Bare flag directives, one per line, comments and key=value dropped."""
    out = set()
    for raw in _config().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line:
            continue
        out.add(line)
    return out


def _fragment_content() -> str:
    tasks = yaml.safe_load(
        (COTURN / "tasks" / "deploy.yml").read_text(encoding="utf-8")
    )
    task = next(t for t in tasks if t.get("name") == _FRAGMENT_TASK)
    return re.sub(r"\s+", " ", task["ansible.builtin.copy"]["content"])


def test_the_fragment_still_declares_udp_on_the_tls_port():
    """The premise of the test below. If this declaration ever goes away the
    DTLS requirement goes with it, and the next test would be pinning a
    directive nothing asks for."""
    assert '{"proto": "udp", "port": coturn_tls_port,' in _fragment_content(), (
        "roles/coturn/tasks/deploy.yml no longer declares udp on "
        "coturn_tls_port; re-check whether DTLS is still wanted"
    )


def test_dtls_is_enabled_because_the_fragment_opens_udp_on_the_tls_port():
    assert "dtls" in _directives(), (
        "turnserver.conf.j2 has no bare `dtls` directive. DTLS client "
        "listeners do not start unless asked for, so tls-listening-port "
        "binds TCP only -- the udp/coturn_tls_port entry in the public-port "
        "fragment opens a firewall port with no listener behind it, and "
        "validate.yml fails on the missing socket"
    )


def test_no_directive_the_pinned_coturn_rejects():
    present = sorted(d for d in _REJECTED if d in _directives())
    assert not present, (
        f"turnserver.conf.j2 carries {present}, which the pinned coturn "
        "rejects. It logs the bad line and keeps running, so the effect is "
        "a silently unapplied setting rather than a failed converge"
    )
