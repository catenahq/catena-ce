"""reconcile/roles/host_maintenance enables the payload's licence-check,
error-report, app-verify and pinned-report timers, and its backup-mode release
and app mount hold boot units.

catena-license, catena-error-report, catena-apps, the update engine and their units ship in the
catena-admin payload; this role only enables each unit, on every host. Whether an absent
unit is a defect depends on whether THIS converge installed the payload: one
that did fails, one whose payload is staged out of band defers and says so, and
nothing touches systemd until the unit is on the host.

Run: uv run pytest tests/unit/test_license_refresh_timer.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "host_maintenance"
TASKS = ROLE / "tasks" / "main.yml"

# Each unit's task-name prefix, the default naming it, the unit the payload
# ships, its suffix and the noun its task names use. A timer is enabled and
# started; a boot unit is enabled and left to run at the next boot.
UNITS = {
    "Licence check": ("license_refresh_systemd_unit_name", "catena-license-refresh", "timer"),
    "Error reports": ("error_report_systemd_unit_name", "catena-error-report", "timer"),
    "App verify": ("app_verify_systemd_unit_name", "catena-app-verify", "timer"),
    "Pinned report": ("pinned_report_systemd_unit_name", "catena-pinned-report", "timer"),
    "Backup-mode release": ("backup_mode_release_systemd_unit_name",
                            "catena-backup-mode-release", "service"),
    "App mount hold": ("app_mount_hold_systemd_unit_name",
                       "catena-app-mount-hold", "service"),
}
BOOT_UNITS = [p for p, u in UNITS.items() if u[2] == "service"]
TIMERS = [p for p, u in UNITS.items() if u[2] == "timer"]


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _index(fragment: str) -> int:
    for i, task in enumerate(_tasks()):
        if fragment in (task.get("name") or ""):
            return i
    raise AssertionError(f"task not found: {fragment!r}")


def _task(fragment: str) -> dict:
    return _tasks()[_index(fragment)]


def _when(task: dict) -> list[str]:
    when = task.get("when") or []
    return [str(c) for c in (when if isinstance(when, list) else [when])]


def _noun(prefix: str) -> str:
    return "timer" if UNITS[prefix][2] == "timer" else "unit"


def _path(prefix: str) -> str:
    var, _, suffix = UNITS[prefix]
    return "/etc/systemd/system/{{ " + var + " }}." + suffix


@pytest.mark.parametrize("prefix", UNITS)
def test_the_unit_name_is_the_one_the_payload_ships(prefix):
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    var, unit, _ = UNITS[prefix]
    assert defaults[var] == unit


@pytest.mark.parametrize("prefix", UNITS)
def test_the_decision_comes_from_the_shared_predicate_for_this_unit(prefix):
    task = _task(f"{prefix}: is the payload-shipped {_noun(prefix)} expected")
    assert task["ansible.builtin.include_role"]["tasks_from"] == "_payload_expected"
    assert task["vars"]["_payload_paths"] == [_path(prefix)]


@pytest.mark.parametrize("prefix", UNITS)
def test_a_missing_unit_fails_a_converge_that_installs_the_payload(prefix):
    task = _task(f"{prefix}: the {_noun(prefix)} is missing")
    assert "ansible.builtin.fail" in task
    assert _when(task) == ["catena_payload_missing | length > 0",
                           "catena_payload_expected | bool"]


@pytest.mark.parametrize("prefix", UNITS)
def test_a_payload_staged_out_of_band_defers_and_says_so(prefix):
    task = _task(f"{prefix}: {_noun(prefix)} staged out of band")
    assert "ansible.builtin.debug" in task
    assert _when(task) == ["catena_payload_missing | length > 0",
                           "not (catena_payload_expected | bool)"]


@pytest.mark.parametrize("prefix", TIMERS)
def test_the_timer_is_enabled_only_once_it_is_on_the_host(prefix):
    """A .timer whose files are absent cannot be enabled, so an ungated enable
    would turn the deferral into the failure it exists to avoid."""
    task = _task(f"{prefix}: enable + start timer")
    unit = task["ansible.builtin.systemd_service"]
    assert unit["name"] == "{{ " + UNITS[prefix][0] + " }}.timer"
    assert unit["enabled"] is True and unit["state"] == "started"
    assert _when(task) == ["not ansible_check_mode",
                           "catena_payload_missing | length == 0"]


@pytest.mark.parametrize("prefix", BOOT_UNITS)
def test_a_boot_unit_is_enabled_not_started_once_it_is_on_the_host(prefix):
    """The unit runs at the next boot: starting it in a converge would run the
    release or the hold against a host that did not just restart."""
    task = _task(f"{prefix}: enable unit")
    unit = task["ansible.builtin.systemd_service"]
    assert unit["name"] == "{{ " + UNITS[prefix][0] + " }}.service"
    assert unit["enabled"] is True and "state" not in unit
    assert _when(task) == ["not ansible_check_mode",
                           "catena_payload_missing | length == 0"]


@pytest.mark.parametrize("prefix", UNITS)
def test_the_unit_tasks_read_their_own_include(prefix):
    """The predicate's facts are overwritten by the next include, so another
    include between this one and the tasks reading it would answer for some
    other path."""
    noun = _noun(prefix)
    start = _index(f"{prefix}: is the payload-shipped {noun} expected")
    end = _index(f"{prefix}: enable " + ("+ start timer" if noun == "timer" else "unit"))
    between = _tasks()[start + 1:end + 1]
    assert start < end
    assert not [t for t in between if "ansible.builtin.include_role" in t]
