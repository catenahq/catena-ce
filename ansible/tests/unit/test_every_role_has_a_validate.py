"""Every role is validated after a converge.

playbooks/validate.yml runs each role's tasks/validate.yml from one literal list.
A role with no validate, or one left off the list, is a role whose result
nothing checks once the converge has run. The list is checked against the two
role roots in both directions, so adding a role without its validate fails here
rather than passing unseen.

The edge verdict _cf_edge_up is set by cloudflare_tunnel's validate, so that role
runs before every role whose validate reads it.

Run: uv run pytest tests/unit/test_every_role_has_a_validate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROOTS = (_ANSIBLE / "bootstrap" / "roles", _ANSIBLE / "reconcile" / "roles")
_VALIDATE = _ANSIBLE / "playbooks" / "validate.yml"


def _roles_on_disk() -> dict[str, Path]:
    return {
        path.name: path
        for root in _ROOTS
        for path in sorted(root.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    }


def _validate_roles() -> list[str]:
    plays = yaml.safe_load(_VALIDATE.read_text())
    vantage1 = next(p for p in plays if p["name"].startswith("Vantage 1 --"))
    return vantage1["vars"]["validate_roles"]


def test_every_role_has_a_validate():
    missing = sorted(
        name for name, path in _roles_on_disk().items()
        if not (path / "tasks" / "validate.yml").is_file()
    )
    assert not missing, f"roles with no tasks/validate.yml: {missing}"


def test_every_role_is_in_validate_roles():
    unlisted = sorted(set(_roles_on_disk()) - set(_validate_roles()))
    assert not unlisted, f"roles validate.yml never runs: {unlisted}"


def test_validate_roles_names_no_role_that_does_not_exist():
    unknown = sorted(set(_validate_roles()) - set(_roles_on_disk()))
    assert not unknown, f"validate_roles names roles that do not exist: {unknown}"


def test_validate_roles_lists_each_role_once():
    roles = _validate_roles()
    assert len(roles) == len(set(roles))


def test_the_tunnel_is_validated_before_every_reader_of_its_verdict():
    roles = _validate_roles()
    readers = sorted(
        name for name, path in _roles_on_disk().items()
        if name != "cloudflare_tunnel"
        and "_cf_edge_up" in (path / "tasks" / "validate.yml").read_text()
    )
    assert "oauth2_proxy" in readers
    late = [r for r in readers if roles.index(r) < roles.index("cloudflare_tunnel")]
    assert not late, f"these validate before the role that sets _cf_edge_up: {late}"
