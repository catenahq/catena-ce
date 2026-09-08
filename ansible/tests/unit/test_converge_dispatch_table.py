"""What the converge's dispatch table is still allowed to carry.

Twenty-four Community actions and twelve Business ones left this repo for the
panel image's dispatch drop-ins. Every command behind them ran a binary the
PAYLOAD installs at a path the payload chose, so this repo was holding an
authorization record for names it does not own and cannot verify -- and a
one-line change to any of them needed an operator with a laptop and an SSH
session against a live client.

What is left is a short list, and it stays short by being DECLARED. Adding a
reserved action here means writing down why the image cannot carry it, which is
the only question worth asking at that moment. The two admissible answers, so
far, are:

  - the action is the way back when a drop-in is broken, so it cannot depend on
    a drop-in;
  - the command is not a payload binary, so an image authorising it would be
    authorising something it does not ship.

An action that has neither answer belongs in catena-admin
payload/actions.d/20-catena.sh.

This replaces test_action_bodies_are_constants.py, whose job was the precondition
for the move -- that every body interpolated product constants rather than host
facts. The bodies moved; that property is now asserted where they live, by
catena-admin payload/actions.d/catena_actions_test.go.

Run: uv run pytest tests/unit/test_converge_dispatch_table.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parents[2] / "roles" / "catena-admin"
DEFAULTS = _ROLE / "defaults" / "main.yml"
CATALOG = _ROLE / "templates" / "actions.yml.j2"

# Every reserved action the converge still authorizes, and why the image cannot.
RESERVED = {
    "ee-install-engines": (
        "it reinstalls the payload, so it cannot depend on the payload having "
        "installed correctly -- this is the way back when a drop-in is broken, "
        "and the reconcile invariant applied to the dispatch table itself"
    ),
    "cloudflare-zones-save": (
        "it pipes through /usr/local/bin/catena-cloudflare-zones, which is not "
        "a payload binary, so an image declaring it would be authorising a "
        "command it does not ship"
    ),
    "cloudflared-check": (
        "it is half of a pair with cloudflared-sync, and splitting the pair "
        "would leave the panel's tunnel path maintained in two repos at once"
    ),
    "cloudflared-sync": (
        "it interpolates cloudflared_tunnel_name, an inventory value -- the "
        "engine's own fallback is the box's hostname, which is not always the "
        "inventory name. It can move when phase 3 puts that value in the store"
    ),
}


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _reserved_actions() -> dict[str, str]:
    """Every reserved action, from every list, keyed on SHAPE.

    Keyed on the variable name pattern rather than on an enumeration, so a new
    list cannot be added and silently escape this gate.
    """
    out: dict[str, str] = {}
    for key, value in _defaults().items():
        if not (key.startswith("catena_admin_") and key.endswith("_reserved_actions")):
            continue
        for action in value or []:
            out[action["name"]] = action["shell"]
    return out


def test_the_reserved_table_is_the_declared_set():
    got = set(_reserved_actions())
    assert got == set(RESERVED), (
        "the converge's reserved dispatch table is not what this file declares. "
        f"added: {sorted(got - set(RESERVED))}, removed: "
        f"{sorted(set(RESERVED) - got)}. An addition needs a reason written "
        "above; if there is not one, the action belongs in the panel image's "
        "drop-in (catena-admin payload/actions.d/20-catena.sh)"
    )


def test_every_reason_is_an_actual_reason():
    """A placeholder reason is worse than none: it reads as though somebody
    thought about it."""
    for name, reason in RESERVED.items():
        assert len(reason) > 60, f"{name} has no real reason: {reason!r}"


def test_the_community_reserved_list_is_gone_not_emptied():
    """An empty list left behind is a place for one of them to come back to.

    The dispatcher is base-first, so a name here would win outright and the
    drop-in where the command is maintained would never be reached -- silently,
    with both files passing every assertion about themselves.
    """
    assert "catena_admin_ce_reserved_actions" not in _defaults()
    body = (_ROLE / "tasks" / "catalog.yml").read_text()
    assert "catena_admin_ce_reserved_actions" not in body


def test_no_reserved_action_is_also_a_catalog_button():
    """The shell-read catalog renders Actions-tab buttons. A reserved name there
    would put a raw button on the page for something whose argument is a base64
    blob, or whose surface is a first-class page with a confirmation."""
    catalog = yaml.safe_load(CATALOG.read_text().split("\n", 1)[1])
    names = {a["name"] for a in (catalog or {}).get("actions", [])}
    assert names, "the canonical catalog parsed empty; this assertion is vacuous"
    overlap = names & set(RESERVED)
    assert not overlap, sorted(overlap)


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
