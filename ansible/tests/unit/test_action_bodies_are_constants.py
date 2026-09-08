"""Every dispatch action's body interpolates product constants and nothing else.

WHY THIS MATTERS NOW. The dispatch table is rendered by the converge, which is
why changing one action's command line needs an operator with a laptop -- the
seam that made a one-line status change unshippable. Moving the table into the
image removes that, and this is the precondition for the move: an action body
can only travel in the image if everything it interpolates is knowable without
the inventory.

It is. All fifteen variables the twenty-eight action bodies interpolate resolve
to fixed absolute paths, and every one of those paths is chosen by the payload
installer itself -- /usr/local/bin/catena-recovery, /var/lib/catena/*.state, the
config store. They are product constants written as Ansible variables, which is
the reason the table is converge-rendered: history, not necessity.

WHY IT MATTERS AFTER THE MOVE. Once the bodies ship in the image they cannot be
rendered against anything. An action that starts interpolating a host fact --
a subdomain, a zone, anything from the store -- would either silently render
empty or have to drag the table back into the converge. This fails the build at
the moment that is proposed, while the alternative is still cheap.

Run: uv run pytest tests/unit/test_action_bodies_are_constants.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROLE = _ANSIBLE / "roles" / "catena-admin"

_JINJA_VAR = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)")

# Values a body may name that are not paths: the runner exports them, so they
# are part of the dispatch contract rather than of the inventory.
_RUNTIME_NAMES = {"PAYLOAD"}


def _defaults() -> dict:
    return yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())


def _common_defaults() -> dict:
    return yaml.safe_load((_ANSIBLE / "roles" / "common" / "defaults" / "main.yml").read_text())


def _actions() -> dict[str, str]:
    d = _defaults()
    out = {}
    for key in ("catena_admin_ce_reserved_actions", "catena_admin_ee_reserved_actions"):
        for action in d.get(key) or []:
            out[action["name"]] = action["shell"]
    return out


def _resolve(var: str):
    for source in (_defaults(), _common_defaults()):
        if var in source:
            return source[var]
    return None


def test_every_action_body_interpolates_only_resolvable_variables():
    unresolved: dict[str, list[str]] = {}
    for name, shell in _actions().items():
        missing = [v for v in sorted(set(_JINJA_VAR.findall(shell)))
                   if v not in _RUNTIME_NAMES and _resolve(v) is None]
        if missing:
            unresolved[name] = missing
    assert not unresolved, (
        "these action bodies name variables that are not defined in the "
        "catena-admin or common role defaults, so what they resolve to depends "
        f"on the inventory: {unresolved}"
    )


def test_every_action_body_interpolates_only_fixed_constants():
    """A constant is a literal string with no Jinja left in it.

    A default that itself reads the inventory -- a dotenv lookup, a cfg_* fact,
    another variable -- is not a constant, it is one more hop to the same
    problem.
    """
    offenders: dict[str, dict[str, str]] = {}
    for name, shell in _actions().items():
        bad = {}
        for var in sorted(set(_JINJA_VAR.findall(shell))):
            if var in _RUNTIME_NAMES:
                continue
            value = _resolve(var)
            if not isinstance(value, str) or "{{" in value or "lookup(" in value:
                bad[var] = repr(value)
        if bad:
            offenders[name] = bad
    assert not offenders, (
        "these action bodies interpolate values that are not fixed constants, "
        "so the dispatch table cannot ship in the image without them rendering "
        f"empty on a client host: {offenders}"
    )


def test_no_action_body_reads_the_store_or_the_inventory():
    """The bodies run ON a host with no Ansible in the picture at all.

    A cfg_* fact is projected by the converge loader and does not exist at
    dispatch time; a dotenv lookup is a file on the operator's laptop. Either
    one in a body is a body that only works while the converge renders it.
    """
    offenders = {}
    for name, shell in _actions().items():
        hits = [t for t in ("cfg_", "dotenv", "hostvars", "ansible_") if t in shell]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        f"action bodies reaching outside the host at dispatch time: {offenders}"
    )


def test_the_paths_they_name_are_owned_by_the_payload():
    """The constants are the payload installer's own choices, which is what
    makes them the image's to know. A body naming a path from somewhere else is
    a body with an owner nobody declared."""
    allowed_prefixes = ("/usr/local/bin/", "/usr/local/sbin/", "/var/lib/catena/",
                        "/etc/catena/", "/run/catena", "/usr/bin/")
    strays: dict[str, list[str]] = {}
    for name, shell in _actions().items():
        bad = []
        for var in sorted(set(_JINJA_VAR.findall(shell))):
            if var in _RUNTIME_NAMES:
                continue
            value = _resolve(var)
            if not isinstance(value, str) or not value.startswith("/"):
                continue  # not a path; the constants test above covers it
            if not value.startswith(allowed_prefixes):
                bad.append(f"{var}={value}")
        if bad:
            strays[name] = bad
    assert not strays, (
        "action bodies naming paths outside the payload's own tree: "
        f"{strays}"
    )
