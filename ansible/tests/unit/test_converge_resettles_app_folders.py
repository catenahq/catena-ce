"""A converge re-settles every application folder against the store.

A primary domain or zone change reaches the host as a converge. The catena-admin
role runs `catena-apps resettle -held`, which rewrites the host-derived values
of each folder application and deploys the ones that moved, inside the hold the
converge already has.

Run: uv run pytest tests/unit/test_converge_resettles_app_folders.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parents[2] / "reconcile/roles/catena-admin/tasks/deploy.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(_DEPLOY.read_text())


def _index(fragment: str) -> int:
    return next(i for i, t in enumerate(_tasks()) if fragment in t.get("name", ""))


def test_the_resettle_runs_inside_the_converges_hold():
    task = _tasks()[_index("settle the application folders against the store")]
    assert task["ansible.builtin.command"]["argv"] == ["/usr/local/bin/catena-apps", "resettle", "-held"]
    assert task["when"] == "_ca_apps_bin.stat.exists"
    assert task["failed_when"] is False
    assert task["no_log"] is True


def test_an_application_that_did_not_settle_is_named_and_does_not_fail_the_converge():
    said = _tasks()[_index("say which applications did not settle")]
    assert "_ca_resettle.rc | default(0) != 0" in said["when"]
    assert "stderr_lines" in said["ansible.builtin.debug"]["msg"]


def test_the_resettle_precedes_the_dashboard_sync_rerun():
    """The routes are read from the labels the redeploy rewrote."""
    assert _index("settle the application folders") < _index("re-run dashboard-sync")
