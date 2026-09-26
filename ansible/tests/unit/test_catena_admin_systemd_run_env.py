"""No dispatch action in this repo runs under systemd-run.

A transient systemd unit starts with a CLEAN environment, and restic refuses to
run without a cache location ($XDG_CACHE_HOME or $HOME). Every dispatch action
that wraps a long-running command in `systemd-run` lives in the panel image's
dispatch drop-ins, where catena-admin payload/actions.d/catena_actions_test.go
asserts each one sets both variables to the same pair the payload's
catena-backup.service uses. An arm declared here instead would carry no such
guard.

Run: uv run pytest tests/unit/test_catena_admin_systemd_run_env.py
"""
from __future__ import annotations

from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]


def test_no_dispatch_action_here_runs_under_systemd_run():
    import yaml

    defaults = yaml.safe_load(
        (ANSIBLE / "reconcile" / "roles" / "catena-admin" / "defaults" / "main.yml").read_text()
    )
    for key, value in defaults.items():
        if not (key.startswith("catena_admin_") and key.endswith("_actions")):
            continue
        for action in value or []:
            if not isinstance(action, dict):
                continue
            assert "systemd-run" not in str(action.get("shell", "")), (
                f"{action['name']} runs under systemd-run from this repo's "
                "table, where nothing checks it hands restic a cache location"
            )
