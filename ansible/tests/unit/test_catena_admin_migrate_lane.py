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

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"
# The bootstrap-side half of the same panel: the runner account, the
# sudoers drop-in, the forced command. Phase 1b made it a role of its
# own so the boundary it was declared on could actually be held.
_HOST_ROLE = _ROLE.parent / "catena_admin_host"
_HOST_DEFAULTS = _HOST_ROLE / "defaults" / "main.yml"
# And the values both halves read, which belong to neither role.
_GROUP_VARS = (
    _ROLE.parents[1] / "playbooks" / "group_vars" / "all" / "main.yml")
HOST_TASKS = _HOST_ROLE / "tasks" / "main.yml"

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
    """Every variable the panel's converge reads, from all three places it now
    lives.

    Phase 1b split the role: the trust path is roles/catena_admin_host, the
    container is roles/catena-admin, and the values BOTH halves need are in
    group_vars because a role default is only dependable once that role has run.
    Merged here so an assertion is about the panel's configuration rather than
    about which file happens to hold a line today.
    """
    merged: dict = {}
    for path in (_GROUP_VARS, DEFAULTS, _HOST_DEFAULTS):
        merged.update(yaml.safe_load(path.read_text()) or {})
    return merged


def _ee_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ee_reserved_actions"]}


def test_the_lane_port_is_declared_once():
    assert int(_defaults()["catena_migrate_lane_port"]) == 9040


def test_the_migration_actions_left_this_repo():
    """They live in the payload drop-in now. A name left behind here would be
    dispatched by the converge's own case arm, and the drop-in -- which is where
    the command is maintained -- would never be reached.

    The lane's paths and state files went with them: they were product
    constants written as Ansible variables, and a constant is owned by whoever
    ships the thing it points at. That the two state files stay distinct from
    the restore machine's is asserted in catena-admin now, where both pairs are
    declared."""
    assert not MIGRATE_ACTIONS & set(_ee_actions())
    for gone in ("catena_migrate_lane_bin", "catena_migration_state_file",
                 "catena_migration_history_file"):
        assert gone not in _defaults(), (
            f"{gone} is back in this repo; the binary and its state files "
            "belong to the payload that installs them"
        )


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
    # Both are product constants now -- the panel's port stopped being a dotenv
    # lookup when phase 3 recognised that no inventory had ever varied it. Read
    # rather than retyped, so a bump of either side is caught here instead of on
    # a host where two things then fight over one port.
    d = _defaults()
    panel = str(d["catena_admin_ui_port"])
    assert panel.isdigit(), (
        f"the panel port is no longer a literal: {panel!r}. If it went back to "
        "being inventory-sourced, the host can no longer converge itself "
        "without an operator's file"
    )
    assert int(d["catena_migrate_lane_port"]) != int(panel)
