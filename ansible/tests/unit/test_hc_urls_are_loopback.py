"""Host-side Healthchecks pings must not leave the host.

Every one of these URLs is dialled by a systemd unit ON the box to reach a
container ON the box. Building them from the public hostname sent the request
out to Cloudflare's edge and back in through the tunnel, so the backup
dead-man depended on the very thing it exists to report on: a tunnel or DNS
failure silenced the alarm instead of tripping it.

A host that is gone is reported by the heartbeat lane's call to the off-site
address the client saves in Settings, not by any of these.

Run: uv run pytest tests/unit/test_hc_urls_are_loopback.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"
ADMIN_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "catena-admin" / "defaults" / "main.yml"
INFRA_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "defaults" / "main.yml"
EXAMPLE_INVENTORY = (
    ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
)

# Pinged by a unit running on the host, each at the check catena-admin's
# catena-schedule keeps on the lanes' schedule (payload/engines/schedule), by
# the role defaults that own each URL.
HOST_SIDE = {
    BACKUP_DEFAULTS: {
        "backup_healthcheck_url": "catena-backup-succeeded",
        "backup_healthcheck_attempted_url": "catena-backup-attempted",
        "offsite_healthcheck_url": "catena-offsite-copy-succeeded",
        "offsite_healthcheck_attempted_url": "catena-offsite-copy-attempted",
    },
    ADMIN_DEFAULTS: {
        "daily_healthcheck_chain_url": "catena-daily",
    },
}


def _defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _host_side():
    for path, urls in HOST_SIDE.items():
        defaults = _defaults(path)
        for key, slug in urls.items():
            yield key, slug, defaults[key]


def test_the_ping_base_is_the_loopback_publish():
    infra = _defaults(INFRA_DEFAULTS)
    assert infra["healthchecks_ping_base"] == (
        "http://127.0.0.1:{{ healthchecks_host_localhost_port }}"
    )


def test_every_host_side_ping_builds_from_the_ping_base():
    for key, slug, value in _host_side():
        assert "healthchecks_ping_base" in value, (
            f"{key} does not build from healthchecks_ping_base: {value}"
        )
        assert "healthchecks_hostname" not in value, (
            f"{key} still routes a host-local ping through the public "
            f"hostname: {value}"
        )
        assert f"/{slug}?create=1" in value, f"{key} does not ping {slug}: {value}"


def test_no_host_side_ping_reads_an_override():
    """The checks these ping are the ones catena-schedule keeps on the lanes'
    schedule. A stored URL pointing one elsewhere would leave catena-schedule
    managing a check nothing pings."""
    for key, _, value in _host_side():
        assert "cfg_" not in value, f"{key} reads a stored override: {value}"
        assert "lookup('dotenv'" not in value, (
            f"{key} reads .env directly: {value}"
        )


def test_no_inventory_reintroduces_a_hostname_ping():
    """An inventory group_vars entry outranks the role default, so a copy
    there silently reverts this."""
    inventory = _defaults(EXAMPLE_INVENTORY)
    for path, urls in HOST_SIDE.items():
        for key in urls:
            assert key not in inventory, (
                f"{EXAMPLE_INVENTORY.name} overrides {key}; "
                f"{path.relative_to(ANSIBLE).parent} owns it"
            )
