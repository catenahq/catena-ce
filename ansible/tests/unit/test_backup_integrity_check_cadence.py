"""The repo integrity check follows the backup; the bit-rot read is a lane.

The metadata check is tied to the backup itself, not to the RESTIC_CHECK state
inside the catena-daily chain: that chain is the licensed lane, so a check
reachable only from there leaves a Community host writing a backup every week
and never checking the repository at all.

Two halves, two cadences, for reasons that differ:

  - metadata (`restic check`): index + structure, no pack reads. Cheap, and it
    validates what the backup just wrote, so it belongs immediately after a
    successful backup -- which also means it runs on every edition.
  - subset (`restic check --read-data-subset=5%`): reads pack data end to end
    to catch silent corruption in the object store. Time-based, because bit
    rot accrues with wall-clock time rather than with backup count, and each
    run pulls 5% of the repo out of object storage.

The subset's schedule lives in the catena-schedule lane table, NOT in a
template and NOT in a `date -u +%u` inside the wrapper -- so it is
client-movable like every other lane.

This file holds the half that asserts about THIS repo: the role defaults and
backup.env. The wrapper's half is catena-admin
payload/lanes/backup_run_integrity_test.go, and the units' half (the bit-rot
timer carries no schedule, its service takes the lock and the restic cache) is
payload/lanes/backup_units_test.go, beside the units themselves. Each test
lives with the artifact it describes; a test that spanned both repos could only
ever run on a workstation with the whole catena workspace checked out.

Run: uv run pytest tests/unit/test_backup_integrity_check_cadence.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "backup"
DEFAULTS = ROLE / "defaults" / "main.yml"
TEMPLATES = ROLE / "templates"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


# --- the post-backup metadata check -----------------------------------------

def test_the_check_is_bounded() -> None:
    """Metadata-only, but it still talks to the object store; a network stall
    must not hold the backup unit open indefinitely. That the WRAPPER reads the
    timeout is asserted in catena-admin; this is the value it reads."""
    assert _defaults()["backup_restic_check_timeout"] > 0


def test_the_script_path_reaches_the_wrapper_through_backup_env() -> None:
    env = (TEMPLATES / "backup.env.j2").read_text()
    assert "BACKUP_RESTIC_CHECK_SCRIPT={{ backup_restic_check_script }}" in env
    assert "BACKUP_RESTIC_CHECK_TIMEOUT={{ backup_restic_check_timeout }}" in env
    assert _defaults()["backup_restic_check_script"].startswith("/usr/local/bin/")


# --- the bit-rot lane -------------------------------------------------------

def test_the_subset_schedule_is_not_an_ansible_default() -> None:
    """Moving the immovable value from the wrapper into a role default would
    not have fixed anything -- it belongs where every other lane's cadence
    lives."""
    assert "backup_restic_check_subset_oncalendar" not in _defaults()
