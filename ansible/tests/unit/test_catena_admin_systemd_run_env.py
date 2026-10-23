"""Every dispatch action in this repo that runs under systemd-run is declared
restic-free.

A transient systemd unit starts with a CLEAN environment, and restic refuses to
run without a cache location ($XDG_CACHE_HOME or $HOME). Every dispatch action
that wraps a restic run in `systemd-run` lives in the panel image's dispatch
drop-ins, where catena-admin payload/actions.d/catena_actions_test.go asserts
each one sets both variables to the same pair the payload's
catena-backup.service uses. An arm in this repo carries no such guard, so one
that runs under systemd-run is DECLARED below, one reviewed entry at a time, as
running no restic.

Run: uv run pytest tests/unit/test_catena_admin_systemd_run_env.py
"""
from __future__ import annotations

from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]

# The arms here that run under systemd-run, and why no repository is involved.
NO_RESTIC_UNDER_SYSTEMD_RUN = {
    "ee-install-engines": (
        "reinstalls the host engines with docker create, docker cp and the "
        "payload's installer; no repository is involved"
    ),
}


def _actions() -> list[dict]:
    import yaml

    defaults = yaml.safe_load(
        (ANSIBLE / "reconcile" / "roles" / "catena-admin" / "defaults" / "main.yml").read_text()
    )
    out: list[dict] = []
    for key, value in defaults.items():
        if not (key.startswith("catena_admin_") and key.endswith("_actions")):
            continue
        out.extend(a for a in value or [] if isinstance(a, dict))
    return out


def test_every_systemd_run_action_here_is_declared_restic_free():
    detached = set()
    for action in _actions():
        shell = str(action.get("shell", ""))
        if "systemd-run" not in shell:
            continue
        detached.add(action["name"])
        assert action["name"] in NO_RESTIC_UNDER_SYSTEMD_RUN, (
            f"{action['name']} runs under systemd-run from this repo's "
            "table, where nothing checks it hands restic a cache location"
        )
        assert "restic" not in shell, (
            f"{action['name']} is declared restic-free and names restic"
        )
    stale = set(NO_RESTIC_UNDER_SYSTEMD_RUN) - detached
    assert not stale, f"declared but not run under systemd-run here: {sorted(stale)}"
