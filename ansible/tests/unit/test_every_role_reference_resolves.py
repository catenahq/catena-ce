"""Every role this tree names has to exist, by name and by path.

WHAT THIS IS FOR. Phase 1b of the converge migration splits `roles/` into
sibling `bootstrap/` and `reconcile/` trees. boundary.yml already declares which
side each role is on, so the move is mechanical -- but 186 files reference a role
somewhere, and Ansible's failure mode for a reference that no longer resolves is
not a parse error. A `roles/<name>` path interpolated into an include that never
runs is silent until the day it runs, and a role name it cannot find is an error
at the moment of execution, which on an install path means partway through a
converge on a real host.

The only complete proof is an install from zero. This is the part that does not
need one: every name resolves, every path exists, and no name resolves to two
places. A rename that breaks one of those breaks it here, in a second, instead
of eleven roles into a bench run.

It is deliberately blind to WHICH root a role lives under. That is boundary.yml's
question and test_converge_boundary.py's assertion; this file only asks whether
the thing named is there.

Run: uv run pytest tests/unit/test_every_role_reference_resolves.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
# Any dot-directory: .collections is the vendored galaxy tree, .ansible is
# whatever ansible-galaxy last installed for a run, and neither is this
# repo's to assert about. __pycache__ is not hidden and has to be named.
_SKIP_DIRS = {"__pycache__"}


def _skipped(path) -> bool:
    return any(part.startswith(".") or part in _SKIP_DIRS for part in path.parts)

# `<side>/roles/<name>` as a path, wherever it appears: a templated include
# ({{ role_path }}/../<name>/tasks/x.yml is the other shape and is relative, so
# it is not a name this can check), a defaults value naming a file to read, a
# script path.
#
# The SIDE is part of the pattern since phase 1b, and deliberately so. A bare
# `roles/<name>/` resolved before the split and resolves to nothing now, so
# matching it too would let the gate pass a path that is already broken -- and
# requiring the prefix is what makes a leftover reference fail here instead of
# on a host. test_a_prefixless_role_path_is_not_accepted holds that.
_ROLE_PATH = re.compile(r"(?<![\w./-])(?:bootstrap|reconcile)/roles/([a-z][a-z0-9_-]*)/")

# The shape that no longer resolves: `roles/<name>/` with nothing in front of
# it. Kept separate so the failure can say which of the two problems it is.
_PREFIXLESS_ROLE_PATH = re.compile(
    r"(?<![\w./-])(?<!bootstrap/)(?<!reconcile/)roles/([a-z][a-z0-9_-]*)/")


def _role_roots() -> list[Path]:
    """Every directory named `roles` in the tree, which is the set the rename
    grows from one to two. Discovered rather than listed, so the gate keeps
    working across the move without being edited in the same commit."""
    out = [p for p in _ANSIBLE.rglob("roles")
           if p.is_dir() and not _skipped(p)]
    assert out, "no roles/ directory found; this gate would pass over nothing"
    return out


def _roles_on_disk() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for root in _role_roots():
        for child in root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                found.setdefault(child.name, []).append(child)
    return found


def _yaml_files() -> list[Path]:
    out = []
    for path in _ANSIBLE.rglob("*"):
        if path.suffix not in {".yml", ".yaml", ".j2"} or not path.is_file():
            continue
        if _skipped(path):
            continue
        if path.stat().st_size > 300_000:
            continue
        out.append(path)
    return out


def _named_roles() -> dict[str, set[str]]:
    """role name -> the files that name it, from the three ways a play can."""
    out: dict[str, set[str]] = {}

    def note(name, path):
        if isinstance(name, str) and name and "{{" not in name:
            out.setdefault(name, set()).add(str(path.relative_to(_ANSIBLE)))

    for path in _ANSIBLE.rglob("*.yml"):
        if _skipped(path) or not path.is_file():
            continue
        if path.stat().st_size > 300_000:
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
        except yaml.YAMLError:
            continue
        for node in _walk(doc):
            if not isinstance(node, dict):
                continue
            for entry in node.get("roles") or []:
                note(entry["role"] if isinstance(entry, dict) else entry, path)
            for key in ("include_role", "import_role",
                        "ansible.builtin.include_role", "ansible.builtin.import_role"):
                spec = node.get(key)
                if isinstance(spec, dict):
                    note(spec.get("name"), path)
    return out


def _walk(node):
    """Every mapping and sequence member, at any depth."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_no_role_name_resolves_to_two_places():
    """After the split there are two role roots, and Ansible searches them in
    order. A name in both means whichever it finds first wins, silently, and the
    other copy is dead in a way that reads as live."""
    duplicated = {name: [str(p.relative_to(_ANSIBLE)) for p in paths]
                  for name, paths in _roles_on_disk().items() if len(paths) > 1}
    assert not duplicated, (
        f"these role names exist under more than one root: {duplicated}. "
        "Ansible takes the first and the rest are unreachable"
    )


def test_every_named_role_exists():
    on_disk = set(_roles_on_disk())
    named = _named_roles()
    assert named, "no role references found; this gate would pass over nothing"
    missing = {name: sorted(where) for name, where in named.items()
               if name not in on_disk}
    assert not missing, (
        f"these plays name a role that is not on disk: {missing}. Ansible finds "
        "out when the play reaches it, which on an install path is partway "
        "through a converge"
    )


def test_every_role_path_exists():
    """The other half, and the one a rename actually breaks.

    A name is resolved through the role search path and survives a directory
    move. A literal `roles/<name>/...` does not: it is a filesystem path, and
    after the split half of them point at nothing.
    """
    on_disk = set(_roles_on_disk())
    found = 0
    broken: dict[str, set[str]] = {}
    for path in _yaml_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in set(_ROLE_PATH.findall(text)):
            found += 1
            if name not in on_disk:
                broken.setdefault(name, set()).add(str(path.relative_to(_ANSIBLE)))
    assert not broken, (
        "these files name a <side>/roles/<name> path that does not exist: "
        f"{ {k: sorted(v) for k, v in broken.items()} }"
    )
    # Guards the guard. The split changed the shape of every one of these, and a
    # pattern that stopped matching would leave this assertion passing over
    # nothing -- which is exactly the state it was in for the length of the move.
    assert found >= 10, f"only {found} role paths found; the pattern has rotted"


def test_a_prefixless_role_path_is_not_accepted():
    """`roles/<name>/` with no side in front of it resolved before phase 1b and
    resolves to nothing now.

    It is the one kind of stale reference the split creates, it is invisible
    until the line runs, and a gate that only checked the paths it recognises
    would pass a file full of them.
    """
    stale: dict[str, set[str]] = {}
    for path in _yaml_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in set(_PREFIXLESS_ROLE_PATH.findall(text)):
            stale.setdefault(name, set()).add(str(path.relative_to(_ANSIBLE)))
    assert not stale, (
        "these files name a role path with no side in front of it, which has "
        "not resolved since roles/ became bootstrap/roles and "
        f"reconcile/roles: { {k: sorted(v) for k, v in stale.items()} }"
    )
