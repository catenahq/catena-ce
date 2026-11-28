"""What the converge's dispatch table is allowed to carry.

The home for a dispatch action is the panel image's own drop-in: catena-admin
payload/actions.d/20-catena.sh (Community) or 10-paid.sh (licensed). Every
command behind those runs a binary the PAYLOAD installs at a path the payload
chose, so an entry in this repo is an authorization record for a name it
neither owns nor can verify, and a one-line change to any of them needs an
operator with a laptop and an SSH session against a live client.

The converge's table is the catalog of actions.yml.j2 alone. There is no
reserved list: a list left behind, empty or not, is a place for an action to
come back to, and the dispatcher is base-first, so a name there would win
outright and the drop-in where the command is maintained would never be
reached -- silently, with both files passing every assertion about themselves.

Run: uv run pytest tests/unit/test_converge_dispatch_table.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "catena-admin"
DEFAULTS = _ROLE / "defaults" / "main.yml"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def test_no_reserved_action_list_exists():
    """Keyed on the variable name pattern rather than on an enumeration, so a
    list cannot be added under a new name and escape this gate."""
    lists = [key for key in _defaults()
             if key.startswith("catena_admin_") and key.endswith("_reserved_actions")]
    assert not lists, (
        f"{lists} is back. An action belongs in the panel image's drop-in "
        "(catena-admin payload/actions.d/20-catena.sh)"
    )
    assert "_reserved_actions" not in (_ROLE / "tasks" / "catalog.yml").read_text()


def test_the_overlay_the_drop_ins_need_is_still_wired():
    """The drop-ins are only reachable because the dispatcher offers a name this
    table does not carry to the overlay directory before refusing it. Without
    that seam thirty-six action names are simply gone -- and the panel's own
    Settings, Schedules and Restore pages are the ones that go with them."""
    d = _defaults()
    assert d["catena_admin_actions_overlay_dir"] == "/etc/catena/admin-actions.d"
    template = (_ROLE / "templates" / "admin-actions.j2").read_text()
    assert "catena_admin_dispatch_overlay" in template
    assert "catena_admin_actions_overlay_dir" in template
