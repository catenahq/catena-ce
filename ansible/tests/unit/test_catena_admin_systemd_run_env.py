"""The restic cache location this repo's half of the contract has to keep.

A transient systemd unit starts with a CLEAN environment. restic refuses to run
without one:

    unable to open cache: unable to locate cache directory:
    neither $XDG_CACHE_HOME nor $HOME are defined

Every dispatch action that wraps a long-running command in `systemd-run` is
exposed to that, and all of them -- restore, migrate, self-update -- now live in
the panel image's dispatch drop-ins, where catena-admin
payload/actions.d/catena_actions_test.go asserts that each one sets both
variables and that both come from one declared pair.

What is left here is the OTHER end of the pair. catena-backup.service sets the
same two values so that both callers share one warm cache instead of each
filling its own, and there is no build-time path from that template to the
drop-ins -- the vendored tree exists only while an image is being built. So each
repo pins the literals it ships and names the other, and the two halves are
compared by whoever changes one.

This went unnoticed until `wizard_restore_smoke` first reached stage 4 in run
2026-08-01T14-47-54-569d: every restore started from the wizard died in about a
second. catena-backup.service had carried the fix, and the comment explaining
it, since it was written -- the lesson was learned once and applied to one of
the two places restic runs under systemd.

Run: uv run pytest tests/unit/test_catena_admin_systemd_run_env.py
"""
from __future__ import annotations

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_UNIT = (
    ANSIBLE / "roles" / "backup" / "templates" / "catena-backup.service.j2"
)

# The shared pair. catena-admin payload/actions.d/catena_actions_test.go carries
# the same two literals and asserts every systemd-run arm uses them.
SHARED_CACHE = {
    "HOME": "/var/lib/catena/restic-home",
    "XDG_CACHE_HOME": "/var/lib/catena/restic-cache",
}


def _backup_unit_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in BACKUP_UNIT.read_text().splitlines():
        m = re.match(r"\s*Environment=(HOME|XDG_CACHE_HOME)=(\S+)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


def test_the_backup_unit_sets_both_variables():
    """Guards the guard: a renamed or dropped Environment= line would leave the
    comparison below with nothing to compare."""
    assert set(_backup_unit_env()) == {"HOME", "XDG_CACHE_HOME"}, (
        f"catena-backup.service no longer sets both; read {_backup_unit_env()}"
    )


def test_the_backup_unit_uses_the_shared_cache_location():
    """One warm cache, not one per caller -- and no silent divergence.

    Changing either value means changing both repos in the same breath. That is
    the cost of the two halves living apart, and it is cheaper than the panel
    and the backup lane each rebuilding a multi-gigabyte cache."""
    assert _backup_unit_env() == SHARED_CACHE, (
        "catena-backup.service and the panel image's dispatch drop-ins would "
        "no longer share a restic cache. If this is deliberate, the matching "
        "change is in catena-admin payload/actions.d/20-catena.sh and "
        "10-business.sh, whose constants are asserted against these same "
        "literals"
    )


def test_no_dispatch_action_here_runs_under_systemd_run():
    """They all moved. One left behind would be the one place the pair above is
    not asserted -- this file can no longer see the arms, so an arm here would
    have no guard at all."""
    import yaml

    defaults = yaml.safe_load(
        (ANSIBLE / "roles" / "catena-admin" / "defaults" / "main.yml").read_text()
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
