"""A converge that moves the panel re-runs dashboard-sync after it.

dashboard-sync corrects each client stack's env from the panel's managed env,
and the infrastructure role runs it BEFORE the catena-admin role reconciles
the panel. On a domain change that first run asks the panel as it was and
finds nothing to move; without a second run after the panel rolls, the client
apps stay on the old domain until the next timer tick.

Run: uv run pytest tests/unit/test_panel_move_reruns_dashboard_sync.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_DEPLOY = _ANSIBLE / "reconcile/roles/catena-admin/tasks/deploy.yml"
_SYNC = _ANSIBLE / "reconcile/roles/infrastructure/tasks/dashboard_sync.yml"


def _tasks(path):
    return yaml.safe_load(path.read_text())


def _named(tasks, fragment):
    return next(i for i, t in enumerate(tasks) if fragment in t.get("name", ""))


def test_the_rerun_follows_the_settled_reconcile():
    tasks = _tasks(_DEPLOY)
    rerun = _named(tasks, "re-run dashboard-sync")
    settled = _named(tasks, "has to have settled the drift")
    assert rerun > settled, "the re-run must ask the panel after it rolled"
    task = tasks[rerun]
    unit = task["ansible.builtin.systemd"]
    assert unit["name"] == "catena-dashboard-sync.service"
    assert unit["state"] == "restarted", (
        "started joins a run already in flight and reports work done before "
        "the panel moved"
    )
    assert "_ca_reconcile is changed" in task["when"]


def test_the_rerun_is_gated_on_the_fact_the_infrastructure_role_pins():
    """A host whose reconciler is staged out of band has no unit to restart."""
    task = _tasks(_DEPLOY)[_named(_tasks(_DEPLOY), "re-run dashboard-sync")]
    assert any("_dashboard_sync_present" in w for w in task["when"])
    assert "_dashboard_sync_present" in _SYNC.read_text()
