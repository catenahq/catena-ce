"""Host-side Healthchecks pings must not leave the host.

Every one of these URLs is dialled by a systemd unit ON the box to reach a
container ON the box. Building them from the public hostname sent the request
out to Cloudflare's edge and back in through the tunnel, so the backup
dead-man depended on the very thing it exists to report on: a tunnel or DNS
failure silenced the alarm instead of tripping it.

The two external URLs are the exception and stay external: they are the
box-death half of the two-monitor model, and a loopback ping cannot detect a
host that is gone.

Run: uv run pytest tests/unit/test_hc_urls_are_loopback.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_DEFAULTS = ANSIBLE / "roles" / "backup" / "defaults" / "main.yml"
INFRA_DEFAULTS = ANSIBLE / "roles" / "infrastructure" / "defaults" / "main.yml"
EXAMPLE_INVENTORY = (
    ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
)

# Pinged by a unit running on the host.
HOST_SIDE = [
    "backup_healthcheck_url",
    "backup_healthcheck_attempted_url",
    "backup_worm_healthcheck_url",
    "backup_worm_healthcheck_attempted_url",
    "nextcloud_mirror_healthcheck_url",
    "nextcloud_mirror_healthcheck_attempted_url",
]

# Deliberately off-host: these detect the host being gone.
EXTERNAL = [
    "backup_healthcheck_url_client",
    "backup_healthcheck_url_operator",
]


def _defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_the_ping_base_is_the_loopback_publish():
    infra = _defaults(INFRA_DEFAULTS)
    assert infra["healthchecks_ping_base"] == (
        "http://127.0.0.1:{{ healthchecks_host_localhost_port }}"
    )


def test_every_host_side_ping_builds_from_the_ping_base():
    backup = _defaults(BACKUP_DEFAULTS)
    for key in HOST_SIDE:
        value = backup[key]
        assert "healthchecks_ping_base" in value, (
            f"{key} does not build from healthchecks_ping_base: {value}"
        )
        assert "healthchecks_hostname" not in value, (
            f"{key} still routes a host-local ping through the public "
            f"hostname: {value}"
        )


def test_every_host_side_ping_keeps_its_override():
    """Loopback is the DEFAULT, not a lock-in: an operator pointing a lane at
    an off-host endpoint must still win.

    The override moved from a live `.env` read to the on-box store (the `.env`
    seeds it once on the first converge), so what has to be present is the
    projected `cfg_` fact ahead of the computed loopback URL."""
    backup = _defaults(BACKUP_DEFAULTS)
    for key in HOST_SIDE:
        value = backup[key]
        assert f"cfg_{key} | default('')" in value, key
        # And it has to come FIRST, or the computed URL would win.
        assert value.index(f"cfg_{key}") < value.index("healthchecks_ping_base"), (
            f"{key}: the override must be evaluated before the computed URL"
        )
        assert "lookup('dotenv'" not in value, (
            f"{key} still reads .env directly; post-install config has one "
            "reader, the store"
        )


def test_the_external_dead_mans_are_not_rewritten():
    backup = _defaults(BACKUP_DEFAULTS)
    for key in EXTERNAL:
        value = backup[key]
        assert "healthchecks_ping_base" not in value, (
            f"{key} is the whole-host-outage lane; a loopback ping cannot "
            "detect a host that is gone"
        )
        assert "127.0.0.1" not in value, key


def test_no_inventory_reintroduces_a_hostname_ping():
    """An inventory group_vars entry outranks the role default, so a copy
    there silently reverts this. The shipped example carried exactly such a
    copy, still building the single-lane slug the succeeded/attempted split
    replaced."""
    inventory = _defaults(EXAMPLE_INVENTORY)
    for key in HOST_SIDE:
        assert key not in inventory, (
            f"{EXAMPLE_INVENTORY.name} overrides {key}; roles/backup/defaults "
            "owns it"
        )
