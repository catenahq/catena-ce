"""Lock down the cloudflared network set-reconcile (backlog 1ae).

The add-if-missing shape only fired when catena-network was absent, so
a host straddling the legacy dokploy-network kept the orphan forever.
These tests pin the convergent shape: one update task that adds
catena-network when missing AND prunes every non-catena attachment by
ID, firing on EITHER condition, with no legacy-name inspect.

Run: uv run pytest tests/unit/test_cloudflared_network_reconcile.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "cloudflare_tunnel" / "tasks" / "main.yml"
)


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for task in _tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_reconcile_targets_exact_set():
    task = _find("Reconcile cloudflared network attachments")
    argv = task["ansible.builtin.command"]["argv"]
    assert "--network-add=catena-network" in argv
    assert "--network-rm=" in argv, (
        "the reconcile must prune stale attachments in the SAME update "
        "(one connector roll), not only add catena-network"
    )
    # Prune is ID-driven from the live spec, not a hardcoded legacy name.
    assert "_stale_net_ids" in argv
    assert "dokploy-network" not in argv


def test_reconcile_fires_on_either_add_or_prune():
    task = _find("Reconcile cloudflared network attachments")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else when
    assert "_needs_catena" in cond
    assert "_stale_net_ids" in cond
    assert " or " in cond, (
        "the update must fire when catena-network is missing OR a stale "
        "attachment lingers -- gating only on the former is the exact "
        "1ae regression (straddled host never pruned)"
    )


def test_needs_catena_compared_as_bool():
    # Jinja var interpolation yields the STRING "False", which is truthy
    # in a bare `when:`; the condition must coerce with | bool.
    task = _find("Reconcile cloudflared network attachments")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else when
    assert "_needs_catena | bool" in cond


def test_no_legacy_network_inspect_remains():
    names = [(t.get("name") or "") for t in _tasks()]
    assert not any("dokploy-network is attached" in n for n in names), (
        "the legacy dokploy-network inspect should be gone -- pruning is "
        "ID-driven from the service spec"
    )
