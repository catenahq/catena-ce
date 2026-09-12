"""The converge removes what it stops shipping, and nothing else.

The converge was copy-only. A unit or script withdrawn from a later release was
not copied, and the old one kept running -- a timer nobody ships any more,
firing at a binary nobody builds any more. reconcile/roles/backup still carries
a hand-written `state: absent` for catena-acquire-lock.sh, which is this
problem solved once, by hand, for the one instance somebody noticed; the
comment there records that the stale helper made every unit that found it spin
to its full timeout.

Two rules carry the whole thing, and this file exists because both of them are
about DELETING files on a client host:

  1. delete only what the product has withdrawn from its declaration -- never
     merely "not installed here", which is what a feature switched off looks
     like;
  2. delete only what is still byte-for-byte what a converge wrote -- these
     directories are shared with the panel payload.

The third property is coverage: a path a role writes but does not declare is
unmanaged forever, silently. That one cannot be asserted from the filter, so
the gate at the bottom reads the tasks and the declarations and holds them to
each other.

Run: uv run pytest tests/unit/test_converge_prune.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ANSIBLE / "playbooks" / "filter_plugins"))

from converge_manifest import (  # noqa: E402
    converge_foreign,
    converge_installed,
    converge_prunable,
    converge_withdrawn,
)


def _stat(path, sha, exists=True, recorded=None):
    """One entry of a stat loop's results, as Ansible shapes it."""
    return {
        "item": {"path": path, "sha256": sha if recorded is None else recorded},
        "stat": {"exists": exists, "checksum": sha},
    }


# --- rule 1: withdrawn means undeclared ------------------------------------
def test_a_path_still_declared_is_never_withdrawn():
    previous = {"installed": [{"path": "/usr/local/bin/a", "sha256": "aa"}]}
    assert converge_withdrawn(previous, ["/usr/local/bin/a"]) == []


def test_a_path_dropped_from_the_declaration_is_withdrawn():
    previous = {"installed": [{"path": "/usr/local/bin/gone", "sha256": "aa"}]}
    assert converge_withdrawn(previous, ["/usr/local/bin/kept"]) == [
        {"path": "/usr/local/bin/gone", "sha256": "aa"}]


def test_a_feature_switched_off_is_not_a_withdrawal():
    """THE distinction the whole design rests on.

    A host with clamav disabled declares its units and installs none of them.
    If the diff were taken between the two INSTALLED sets, the next converge
    would read that as a withdrawal and delete the units of a feature the
    client merely turned off -- and turning it back on would leave them gone
    until something re-ran the role."""
    declared = ["/etc/systemd/system/catena-clamav-watch.timer"]
    # Last converge had it on and installed it; this one has it off.
    previous = {
        "declared": declared,
        "installed": [{"path": declared[0], "sha256": "aa"}],
    }
    assert converge_withdrawn(previous, declared) == [], (
        "a declared-but-absent path was treated as withdrawn")


def test_an_absent_path_is_declared_but_not_installed():
    """The record that makes the test above possible."""
    declared = ["/usr/local/bin/here", "/usr/local/bin/not-here"]
    stats = [_stat("/usr/local/bin/here", "aa"),
             _stat("/usr/local/bin/not-here", "", exists=False)]
    assert converge_installed(declared, stats) == [
        {"path": "/usr/local/bin/here", "sha256": "aa"}]


def test_a_missing_previous_manifest_withdraws_nothing():
    """A first converge on a host: nothing was withdrawn because nothing was
    ever recorded."""
    assert converge_withdrawn({}, ["/usr/local/bin/a"]) == []
    assert converge_withdrawn(None, ["/usr/local/bin/a"]) == []
    assert converge_withdrawn({"installed": []}, []) == []


# --- rule 2: delete only what is still ours --------------------------------
def test_a_file_that_still_matches_is_ours_to_remove():
    stats = [_stat("/usr/local/bin/gone", "aa")]
    assert converge_prunable(stats) == [{"path": "/usr/local/bin/gone", "sha256": "aa"}]
    assert converge_foreign(stats) == []


def test_a_file_something_else_rewrote_is_left_alone():
    """These directories are shared with the panel payload, which installs
    /usr/local/bin/catena-* and /etc/systemd/system/catena-* of its own. A
    withdrawn path whose content moved belongs to whoever wrote it."""
    stats = [{"item": {"path": "/usr/local/bin/shared", "sha256": "aa"},
              "stat": {"exists": True, "checksum": "bb"}}]
    assert converge_prunable(stats) == []
    assert converge_foreign(stats) == [{"path": "/usr/local/bin/shared", "sha256": "aa"}]


def test_a_candidate_with_no_recorded_digest_is_never_deleted():
    """"I did not record what I wrote" is not a licence to delete whatever is
    at that path now."""
    stats = [{"item": {"path": "/usr/local/bin/x", "sha256": ""},
              "stat": {"exists": True, "checksum": "bb"}}]
    assert converge_prunable(stats) == []
    assert converge_foreign(stats) == [{"path": "/usr/local/bin/x", "sha256": ""}]


def test_a_candidate_already_gone_is_neither():
    """The ordinary outcome of converging twice: reported as nothing, because
    there is nothing to delete and nothing to say about ownership."""
    stats = [_stat("/usr/local/bin/x", "aa", exists=False)]
    assert converge_prunable(stats) == []
    assert converge_foreign(stats) == []


# --- declaration hygiene ---------------------------------------------------
def test_an_unresolved_declaration_is_dropped_not_recorded():
    """An undefined variable renders as an empty string rather than raising, so
    a declaration that resolved to nothing must not reach the recorded set --
    where it would match nothing and widen the withdrawn list by one."""
    assert converge_installed(["", "  ", "/usr/local/bin/a"],
                              [_stat("/usr/local/bin/a", "aa")]) == [
        {"path": "/usr/local/bin/a", "sha256": "aa"}]


def test_a_relative_declaration_is_refused():
    """A relative path would be resolved against whatever the delete leg's
    working directory happened to be."""
    with pytest.raises(ValueError):
        converge_withdrawn({}, ["usr/local/bin/a"])


def test_declarations_are_deduped_and_ordered():
    stats = [_stat("/usr/local/bin/a", "aa")]
    assert converge_installed(["/usr/local/bin/a", "/usr/local/bin/a"], stats) == [
        {"path": "/usr/local/bin/a", "sha256": "aa"}]


# --- the gate: every managed path is declared ------------------------------
# The prefixes in scope: where a withdrawn file keeps EXECUTING rather than
# merely sitting there. A stale config is wrong; a stale timer is an action.
_MANAGED_PREFIXES = ("/etc/systemd/system/", "/usr/local/bin/", "/usr/local/sbin/")

_WRITE = re.compile(
    r"^\s*dest:\s*[\"']?((?:/|\{\{)[^\"'\n]+)[\"']?\s*$", re.M)


def _role_defaults(role: Path) -> dict:
    path = role / "defaults" / "main.yml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resolve(expr: str, defaults: dict, shared: dict) -> str:
    """Substitute `{{ var }}` from the role's own defaults or the shared
    group_vars, so a templated destination can be compared with a templated
    declaration. Anything still unresolved is returned as written."""
    out = expr.strip().strip('"').strip("'")
    for var in re.findall(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", out):
        value = defaults.get(var, shared.get(var))
        if isinstance(value, str) and "{{" not in value:
            out = re.sub(rf"\{{\{{\s*{var}\s*\}}\}}", value, out)
    return out


def _shared_vars() -> dict:
    path = _ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _reconcile_roles() -> list[Path]:
    root = _ANSIBLE / "reconcile" / "roles"
    return sorted(p for p in root.iterdir() if p.is_dir())


def test_every_reconcile_write_into_a_managed_prefix_is_declared():
    """Coverage, held by rule rather than by memory.

    A role that writes a unit or a host binary and does not declare it gets no
    withdrawal handling, silently and for ever -- which is the state the whole
    tree was in. The failure direction is safe (an undeclared path is never
    deleted), so this gate is the only thing that makes the set complete."""
    shared = _shared_vars()
    missing = []
    for role in _reconcile_roles():
        defaults = _role_defaults(role)
        declared = {_resolve(str(p), defaults, shared)
                    for p in (defaults.get(f"{role.name}_managed_paths") or [])}
        for task_file in (role / "tasks").rglob("*.yml"):
            body = task_file.read_text(encoding="utf-8", errors="ignore")
            for raw in _WRITE.findall(body):
                dest = _resolve(raw, defaults, shared)
                if not dest.startswith(_MANAGED_PREFIXES):
                    continue
                if dest not in declared:
                    missing.append(
                        f"{task_file.relative_to(_ANSIBLE)}: {dest}")
    assert not missing, (
        "these reconcile-side tasks write into a prefix where a withdrawn file "
        "keeps executing, and no role declares them -- so nothing will ever "
        "remove them when the product stops shipping them. Add each to its "
        f"role's <role>_managed_paths:\n  " + "\n  ".join(sorted(set(missing))))


def test_the_gate_reads_real_writes():
    """A gate that matched nothing would pass over a tree full of them."""
    shared = _shared_vars()
    seen = 0
    for role in _reconcile_roles():
        defaults = _role_defaults(role)
        for task_file in (role / "tasks").rglob("*.yml"):
            for raw in _WRITE.findall(
                    task_file.read_text(encoding="utf-8", errors="ignore")):
                if _resolve(raw, defaults, shared).startswith(_MANAGED_PREFIXES):
                    seen += 1
    assert seen > 15, f"only {seen} managed-prefix writes found across the roles"


def test_every_declared_path_is_absolute_once_resolved():
    """A declaration that still carries Jinja after resolution is one whose
    variable this gate could not find -- and at run time it would render empty
    and be dropped, so the role would believe it declared something it did
    not."""
    shared = _shared_vars()
    bad = []
    for role in _reconcile_roles():
        defaults = _role_defaults(role)
        for raw in (defaults.get(f"{role.name}_managed_paths") or []):
            resolved = _resolve(str(raw), defaults, shared)
            if "{{" in resolved or not resolved.startswith("/"):
                bad.append(f"{role.name}: {raw!r} -> {resolved!r}")
    assert not bad, "declared paths that do not resolve to an absolute path:\n  " + \
        "\n  ".join(bad)


def test_both_converge_paths_prune():
    """A prune that ran on only one path would leave a self-converged host
    accumulating exactly what this exists to remove."""
    for name in ("site.yml", "reconcile.yml"):
        play = yaml.safe_load((_ANSIBLE / "playbooks" / name).read_text())[0]
        files = [t.get("ansible.builtin.include_tasks", {}).get("file")
                 for t in play["post_tasks"]]
        assert "tasks/prune_withdrawn_files.yml" in files, (
            f"{name} does not prune withdrawn files")


def test_the_prune_runs_before_the_release_manifest():
    """The manifest says a converge finished. A prune that ran after it would
    be work the manifest already claimed as done."""
    for name in ("site.yml", "reconcile.yml"):
        play = yaml.safe_load((_ANSIBLE / "playbooks" / name).read_text())[0]
        files = [t.get("ansible.builtin.include_tasks", {}).get("file")
                 for t in play["post_tasks"]]
        assert files.index("tasks/prune_withdrawn_files.yml") < \
            files.index("tasks/record_release_manifest.yml"), name


def test_the_manifest_is_not_in_etc():
    """It describes what is on this host right now and it is the input to a
    delete leg. /etc rides the restic snapshot, so a restored copy would
    describe the machine the snapshot came from and hand the delete leg a list
    of paths this host never had."""
    path = _shared_vars()["catena_converge_manifest_path"]
    assert path.startswith("/var/lib/"), path
