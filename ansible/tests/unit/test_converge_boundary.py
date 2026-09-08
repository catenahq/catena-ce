"""The converge boundary, declared in boundary.yml and enforced here.

Two things run against a Catena host: an operator with a laptop, and the host
itself. Until now the line between them was wherever a task happened to have
been written, which is how app config ended up needing an SSH session.

boundary.yml draws the line. This asserts three properties of it.

  1. Every role is on exactly one side, or is declared as straddling with its
     task files enumerated. A role nobody classified is a role whose side gets
     decided by whoever next edits it.

  2. No reconcile-side task writes a bootstrap-owned path. A reconcile that can
     rewrite the forced command is a reconcile that can rewrite what a
     reconcile is, and that is the whole reason the two sides exist.

  3. The number of inventory-sourced variables a reconcile role reads only ever
     goes DOWN. Each one is a value that must move into the on-box store before
     the host can converge itself, so the count is the remaining work, measured
     rather than estimated. A ratchet rather than a pass/fail: it is 18 today
     and the build fails if it becomes 19.

Run: uv run pytest tests/unit/test_converge_boundary.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_BOUNDARY = _ANSIBLE / "boundary.yml"
_ROLES = _ANSIBLE / "roles"

# What the reconcile side still takes from the operator's inventory. Every
# entry is one settings key away from deletion; see the module docstring.
INVENTORY_BACKLOG = 18


def _boundary() -> dict:
    return yaml.safe_load(_BOUNDARY.read_text())


def _bootstrap_names(b: dict) -> set[str]:
    return {r["name"] for r in b["bootstrap_roles"]}


def test_every_role_is_on_exactly_one_side():
    b = _boundary()
    declared = _bootstrap_names(b) | set(b["reconcile_roles"])
    straddling = set(b["straddling_roles"])

    on_disk = {p.name for p in _ROLES.iterdir() if p.is_dir()}
    # The regenerate playbook's role is a variant of cloudflare_tunnel and runs
    # only from its own playbook, never from site.yml.
    on_disk.discard("cloudflare_tunnel_regenerate")
    on_disk.discard("tier1_stack")

    unclassified = sorted(on_disk - declared)
    assert not unclassified, (
        "these roles are on neither side of boundary.yml, so which side they "
        f"are on is decided by whoever next edits them: {unclassified}"
    )

    both = sorted((_bootstrap_names(b) & set(b["reconcile_roles"])) - straddling)
    assert not both, f"declared on both sides without being declared straddling: {both}"


def test_every_bootstrap_role_says_why_it_is_one():
    """A set that grows by assertion grows. Each entry carries its reason, and
    the reason has to be a sentence rather than a word."""
    for role in _boundary()["bootstrap_roles"]:
        why = (role.get("why") or "").strip()
        assert len(why) > 40, (
            f"{role['name']} is bootstrap-owned with no real reason given: {why!r}"
        )


def _side_of(path: Path, b: dict) -> str | None:
    if _ROLES not in path.parents:
        return None
    role = path.relative_to(_ROLES).parts[0]
    straddling = b["straddling_roles"]
    if role in straddling:
        spec = straddling[role]
        for side in ("bootstrap", "reconcile"):
            if path.name in (spec.get(side) or []):
                return side
        return spec["default_side"]
    if role in _bootstrap_names(b):
        return "bootstrap"
    if role in b["reconcile_roles"]:
        return "reconcile"
    return None


def _files_on(side: str, b: dict) -> list[Path]:
    out = []
    for path in _ROLES.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.stat().st_size > 300_000:
            continue
        if _side_of(path, b) == side:
            out.append(path)
    return out


def test_no_reconcile_task_writes_a_bootstrap_owned_path():
    """Task files only, and never the ones an operator playbook owns.

    A defaults file that names /etc/ssh because restic READS it is not a task
    that writes it, and roles/backup does exactly that. A rule that cannot tell
    those apart is one people argue with instead of obeying.
    """
    b = _boundary()
    operator_only = set(b["operator_only_files"])
    offenders: dict[str, list[str]] = {}
    for path in _files_on("reconcile", b):
        rel = str(path.relative_to(_ANSIBLE))
        if "tasks" not in path.parts or rel in operator_only:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hit = [p for p in b["bootstrap_owned_paths"] if p in text]
        if hit:
            offenders[rel] = hit
    assert not offenders, (
        "reconcile-side files reference paths only an operator may write. A "
        "reconcile that can rewrite the way in can rewrite what a reconcile is: "
        f"{offenders}"
    )


def _inventory_vars() -> dict[str, str]:
    """Every variable defined from the operator's .env, wherever it is defined."""
    out: dict[str, str] = {}
    sources = [_ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"]
    sources += sorted(_ROLES.glob("*/defaults/main.yml"))
    for src in sources:
        text = src.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"^([a-z][a-z0-9_]*):((?:.|\n)*?)(?=^\S|\Z)", text, re.M):
            if "dotenv" in m.group(2):
                out[m.group(1)] = str(src.relative_to(_ANSIBLE))
    return out


def _backlog() -> dict[str, set[str]]:
    b = _boundary()
    inventory = _inventory_vars()
    hits: dict[str, set[str]] = {}
    for path in _files_on("reconcile", b):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for var in inventory:
            if re.search(r"\b" + var + r"\b", text):
                hits.setdefault(var, set()).add(path.relative_to(_ROLES).parts[0])
    return hits


def test_the_inventory_backlog_only_shrinks():
    """The count of inventory values the reconcile side still needs.

    Not zero yet, and it is not supposed to be: emptying it is the phase this
    number exists to size. What must not happen is a new one appearing, because
    that is a value being added to the set the host cannot converge without an
    operator for.
    """
    found = _backlog()
    assert len(found) <= INVENTORY_BACKLOG, (
        f"the reconcile side now reads {len(found)} inventory variables, up from "
        f"{INVENTORY_BACKLOG}. Each one is a value the host cannot get for "
        "itself, so this number is the distance to a self-converging host: "
        f"{ {k: sorted(v) for k, v in sorted(found.items())} }"
    )
    if len(found) < INVENTORY_BACKLOG:
        raise AssertionError(
            f"the backlog is down to {len(found)}; lower INVENTORY_BACKLOG to "
            "match so the ratchet keeps holding"
        )


def test_the_reconcile_playbook_matches_the_declaration():
    """Two files naming the same list is one of them going stale. The playbook
    is what runs; boundary.yml is what the gates above read."""
    playbook = _ANSIBLE / "playbooks" / "reconcile.yml"
    plays = yaml.safe_load(playbook.read_text())
    roles = [r["role"] if isinstance(r, dict) else r
             for play in plays for r in (play.get("roles") or [])]
    assert roles == _boundary()["reconcile_roles"], (
        "playbooks/reconcile.yml and boundary.yml disagree about which roles "
        f"reconcile, or about their order: {roles}"
    )
