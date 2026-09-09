"""One predicate for "is the payload expected on this host", not five.

The question was asked in five places and answered two ways. reconcile/roles/payload
publishes `catena_payload_engines_expected` during a converge, so
reconcile/roles/cloudflare_tunnel and reconcile/roles/backup/tasks/install.yml read the fact; a
standalone validate play cannot see a set_fact from a role that is not in its
play, so reconcile/roles/backup/tasks/validate.yml read the env var. Both were right
locally, and the divergence was not cosmetic -- only the validate side also
accepted "or the file is already here", so the converge side could not tell
NOT YET from PARTIALLY INSTALLED, which is the one case worth catching.

These pin that there is now one implementation, that every caller uses it, and
that it still answers correctly for both kinds of play.

Run: uv run pytest tests/unit/test_payload_expected_predicate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
SHARED = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "_payload_expected.yml"

# Every place that has to make this decision. A sixth that hand-rolls it is the
# regression this file exists to catch.
CALLERS = {
    "reconcile/roles/cloudflare_tunnel/tasks/main.yml": ["{{ cloudflared_sync_bin }}"],
    "reconcile/roles/backup/tasks/install.yml": ["{{ backup_wrapper_script }}"],
    "reconcile/roles/backup/tasks/validate.yml": [
        "{{ backup_wrapper_script }}",
        "{{ backup_coverage_script }}",
        "{{ backup_restic_env_script }}",
        "{{ backup_snapshot_list_script }}",
    ],
    # The lib dir rides along because the reconciler imports four modules from
    # it at module scope. /usr/local/bin is in backup_paths and the modules
    # arrive with the payload, so the two can land separately -- and a host
    # holding the binary alone answered "the engines are here" and then died
    # inside systemd on ModuleNotFoundError.
    "reconcile/roles/infrastructure/tasks/dashboard_sync.yml":
        "{{ dashboard_sync_required_paths }}",
    "reconcile/roles/infrastructure/tasks/validate.yml":
        "{{ dashboard_sync_required_paths }}",
}


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _includes(path: Path) -> list[dict]:
    return [
        t for t in _tasks(path)
        if (t.get("ansible.builtin.include_role") or {}).get("tasks_from")
        == "_payload_expected"
    ]


def _decide() -> dict:
    for t in _tasks(SHARED):
        if "decide" in (t.get("name") or ""):
            return t["ansible.builtin.set_fact"]
    raise AssertionError("the decide task is gone")


# ── one implementation ─────────────────────────────────────────────────

def test_every_caller_uses_the_shared_predicate():
    for rel, paths in CALLERS.items():
        incs = _includes(ANSIBLE / rel)
        assert len(incs) == 1, f"{rel}: expected one include, got {len(incs)}"
        assert incs[0]["vars"]["_payload_paths"] == paths, rel


def test_no_caller_still_hand_rolls_the_decision():
    """A second implementation is how the two dialects happened. The raw
    signals belong in the predicate; a caller naming them in CODE is deciding
    for itself again. Comments are stripped -- these files explain the boundary
    at length and the prose is not the decision."""
    for rel in CALLERS:
        text = (ANSIBLE / rel).read_text()
        code = "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())
        assert "catena_payload_engines_expected" not in code, rel
        assert "lookup('env', 'CATENA_PAYLOAD_INSTALL'" not in code, rel


def test_the_predicate_is_the_only_place_the_raw_signals_are_read():
    code = "\n".join(
        ln.split("#", 1)[0] for ln in SHARED.read_text().splitlines())
    assert "catena_payload_engines_expected" in code
    assert "CATENA_PAYLOAD_INSTALL" in code


# ── and it answers correctly for both kinds of play ────────────────────

def test_a_converge_prefers_the_fact_over_the_env():
    """During a converge reconcile/roles/payload has already decided, and its fact is the
    answer for THIS converge. Reading the env var first would answer for the
    process instead -- the same value today, and a divergence the first time a
    play sets it per-task."""
    expr = str(_decide()["catena_payload_expected"])
    fact = expr.index("catena_payload_engines_expected")
    env = expr.index("CATENA_PAYLOAD_INSTALL")
    assert fact < env, "the env var is consulted before the fact"
    assert "default(" in expr, "the fallback must be a default(), not an or"


def test_a_present_file_settles_it_either_way():
    """The leg that started in reconcile/roles/backup/tasks/validate.yml. Without it a
    host holding three of four payload scripts is indistinguishable from one
    the payload has not reached, and the broken install goes unchecked."""
    expr = str(_decide()["catena_payload_expected"])
    assert "catena_payload_present | length > 0" in expr
    assert " or " in expr


def test_missing_and_present_are_both_published():
    """A caller needs `missing` to write a message naming what is absent. A
    deferral that does not say what is deferred reads as a silent skip."""
    for t in _tasks(SHARED):
        facts = t.get("ansible.builtin.set_fact") or {}
        if "catena_payload_missing" in facts:
            assert "catena_payload_present" in facts
            return
    raise AssertionError("the split task is gone")


def test_the_predicate_stats_only_what_the_caller_asked_for():
    """It must not carry its own list of payload paths: that would be a second
    thing to keep in agreement with the image, and the gate in ops
    (audit/payload_owners.py) already derives the real one from the tree."""
    for t in _tasks(SHARED):
        if "ansible.builtin.stat" in t:
            assert t["loop"] == "{{ _payload_paths }}"
            return
    raise AssertionError("the stat task is gone")
