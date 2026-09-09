"""The repo integrity check follows the backup; the bit-rot read is a lane.

Before this split, `catena-restic-check` ran ONLY as the RESTIC_CHECK state
inside the catena-daily chain -- and that chain is the licensed lane. A
Community host therefore wrote a backup every week and never checked the
repository at all. There was no timer for it and no other caller.

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

This file holds the half that asserts about THIS repo: the units, the
templates and the role defaults. The half that read the wrapper moved to
catena-admin payload/lanes/backup_run_integrity_test.go when the wrapper moved
into the image payload. Each test lives with the artifact it describes; a test
that spanned both repos could only ever run on a workstation with the whole
catena workspace checked out.

Run: uv run pytest tests/unit/test_backup_integrity_check_cadence.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "backup"
DEFAULTS = ROLE / "defaults" / "main.yml"
INSTALL = ROLE / "tasks" / "install.yml"
TEMPLATES = ROLE / "templates"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _install_tasks() -> list[dict]:
    return yaml.safe_load(INSTALL.read_text())


def _names(tasks) -> list[str]:
    return [t.get("name", "") for t in tasks]


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

def test_both_bit_rot_units_are_installed() -> None:
    """On EVERY edition. The daily chain is licensed; this is not."""
    names = " ".join(_names(_install_tasks()))
    assert "bit-rot probe service unit" in names
    assert "bit-rot probe timer unit" in names


def test_the_bit_rot_timer_carries_no_schedule_of_its_own() -> None:
    """`catena-schedule apply` owns enable/disable and OnCalendar for every
    lane out of /etc/catena/config.json. A schedule templated into the unit
    would be a second owner, and the two would disagree the moment somebody
    changed the cadence in the panel -- the next converge would put the old
    one back."""
    timer = (TEMPLATES / "catena-restic-check-subset.timer.j2").read_text()
    assert "OnCalendar=" not in timer, (
        "the unit sets its own schedule, which makes it a second owner "
        "alongside catena-schedule"
    )
    assert "Unit=catena-restic-check-subset.service" in timer


def test_the_bit_rot_service_asks_for_the_subset_and_takes_the_lock() -> None:
    svc = (TEMPLATES / "catena-restic-check-subset.service.j2").read_text()
    assert "--subset" in svc, "this unit exists to do the deep read"
    assert "/run/catena.lock" in svc, (
        "a deep read overlapping a backup contends on the repo and on the "
        "disk the backup stages dumps to"
    )
    assert "XDG_CACHE_HOME" in svc and "HOME=" in svc, (
        "without the restic cache the probe re-downloads the repo index every "
        "run, doubling the egress it already costs"
    )


def test_the_subset_schedule_is_not_an_ansible_default() -> None:
    """Moving the immovable value from the wrapper into a role default would
    not have fixed anything -- it belongs where every other lane's cadence
    lives."""
    assert "backup_restic_check_subset_oncalendar" not in _defaults()
