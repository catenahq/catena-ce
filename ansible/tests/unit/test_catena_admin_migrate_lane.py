"""Lock the migration lane's CE-side wiring.

The lane itself is a host process in the private repo. What lives here is
everything that decides how it is reachable and how it is armed, and each
of those has a way of going quietly wrong:

  1. The port has to be declared tailnet-only in the public-port
     registry. The lane binds its tailnet address and refuses to start
     without one, so the declaration is defence in depth -- but it is
     ALSO what puts the port in the effective set validation reads. An
     undeclared listener reads as an unexpected open port.
  2. The eight action names are NOT in either of this repo's dispatch
     lists any more. They moved into the payload's own drop-in
     (catena-admin payload/actions.d/10-business.sh), because every
     command behind them is a binary the payload installs at a path the
     payload chose -- so this repo was authorizing names it does not own
     and cannot verify. What each command must LOOK like is asserted
     there; what is asserted here is that they left, because a name in
     both places is dispatched by the converge's arm and the drop-in is
     never reached.
  3. The lane's state files stay separate from the restore machine's. A
     migration DRIVES a restore, so one shared file would have the page
     watching the move read the restore's progress as its own.

Run: uv run pytest tests/unit/test_catena_admin_migrate_lane.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"
HOST_TASKS = _ROLE / "tasks" / "host.yml"

MIGRATE_ACTIONS = {
    "catena-migrate-arm",
    "catena-migrate-disarm",
    "catena-migrate-status",
    "catena-migrate-resume",
    # Target side: this server pulling another one onto itself.
    "catena-migrate-run",
    "catena-migrate-run-status",
    "catena-migrate-run-reset",
    "catena-migrate-resume-source",
}


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _ee_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ee_reserved_actions"]}


def _ce_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ce_reserved_actions"]}


def test_lane_paths_are_declared_once():
    d = _defaults()
    assert d["catena_migrate_lane_bin"].endswith("/catena-migrate-lane")
    assert int(d["catena_migrate_lane_port"]) == 9040


def test_the_migration_actions_left_this_repo():
    """They live in the payload drop-in now. A name left behind here would be
    dispatched by the converge's own case arm, and the drop-in -- which is
    where the command is maintained -- would never be reached."""
    ee, ce = set(_ee_actions()), set(_ce_actions())
    assert not MIGRATE_ACTIONS & ee, sorted(MIGRATE_ACTIONS & ee)
    assert not MIGRATE_ACTIONS & ce, sorted(MIGRATE_ACTIONS & ce)


def test_the_overlay_the_payload_needs_is_still_wired():
    """The drop-in is only reachable because the dispatcher offers an unknown
    name to the overlay directory before refusing it. Without that seam the
    eight names above are simply gone."""
    d = _defaults()
    assert d["catena_admin_actions_overlay_dir"] == "/etc/catena/admin-actions.d"
    template = (_ROLE / "templates" / "admin-actions.j2").read_text()
    assert "catena_admin_dispatch_overlay" in template
    assert "catena_admin_actions_overlay_dir" in template


def test_target_side_state_files_are_separate_from_the_restores():
    # A migration DRIVES a restore, so sharing one state file would have the
    # page watching the move read the restore's progress as its own.
    d = _defaults()
    assert d["catena_migration_state_file"] != d["catena_recovery_state_file"]
    assert d["catena_migration_history_file"] != d["catena_recovery_history_file"]
    # Under /var/lib, not /etc: /etc is in the backup set, and a snapshot taken
    # mid-move would otherwise carry a half-finished migration's state into the
    # next host that restored it.
    for key in ("catena_migration_state_file", "catena_migration_history_file"):
        assert d[key].startswith("/var/lib/catena/"), d[key]


def test_lane_port_is_declared_tailnet_only():
    body = HOST_TASKS.read_text()
    assert "catena_migrate_lane_port" in body, (
        "the lane's port is not declared in the public-port registry, so "
        "validation would read it as an unexpected open port"
    )
    # The declaration block: the lane is a host process, so bind=host (not the
    # docker DNAT path the panel's published port uses), and tailnet scope.
    start = body.index("declare UI + migration-lane ports")
    block = body[start:start + 1400]
    assert '"port": catena_migrate_lane_port | int' in block
    assert '"scope": "tailnet"' in block
    assert '"bind": "host"' in block
    assert '"scope": "any"' not in block


def test_lane_port_does_not_collide_with_the_panel_port():
    # The panel's port is a dotenv lookup with a default; the lane's is a
    # literal. Compare against that default, parsed out rather than retyped, so
    # a bump of either side is caught here instead of on a host where two
    # things then fight over one port.
    d = _defaults()
    m = re.search(r"default='(\d+)'", str(d["catena_admin_ui_port"]))
    assert m, f"could not read the panel port default: {d['catena_admin_ui_port']!r}"
    assert int(d["catena_migrate_lane_port"]) != int(m.group(1))
