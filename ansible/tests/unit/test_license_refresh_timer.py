"""reconcile/roles/host_maintenance enables the payload's licence-check,
error-report, app-verify and pinned-report timers, and its backup-mode release
and app mount hold boot units.

catena-license, catena-error-report, catena-apps, the update engine and their units ship in the
catena-admin payload; this role only enables each unit, on every host, through
tasks/_payload_unit.yml. Whether an absent unit is a defect depends on whether
THIS converge installed the payload: one that did fails, one whose payload is
staged out of band defers and says so, and nothing touches systemd until the
unit is on the host.

Run: uv run pytest tests/unit/test_license_refresh_timer.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "host_maintenance"
MAIN = ROLE / "tasks" / "main.yml"
UNIT_TASKS = ROLE / "tasks" / "_payload_unit.yml"

# The default naming each unit, the unit the payload ships and its suffix. A
# timer is enabled and started; a boot unit is enabled and left to run at the
# next boot.
UNITS = {
    "license_refresh_systemd_unit_name": ("catena-license-refresh", "timer"),
    "error_report_systemd_unit_name": ("catena-error-report", "timer"),
    "app_verify_systemd_unit_name": ("catena-app-verify", "timer"),
    "pinned_report_systemd_unit_name": ("catena-pinned-report", "timer"),
    "backup_mode_release_systemd_unit_name": ("catena-backup-mode-release", "service"),
    "app_mount_hold_systemd_unit_name": ("catena-app-mount-hold", "service"),
}


def _unit_tasks() -> list[dict]:
    return yaml.safe_load(UNIT_TASKS.read_text())


def _entries() -> dict[str, dict]:
    (loop,) = [t for t in yaml.safe_load(MAIN.read_text())
               if t.get("ansible.builtin.include_tasks") == "_payload_unit.yml"]
    return {e["unit"]: e for e in loop["loop"]}


def _task(module: str) -> dict:
    (task,) = [t for t in _unit_tasks() if f"ansible.builtin.{module}" in t]
    return task


def _when(task: dict) -> list[str]:
    when = task.get("when") or []
    return [str(c) for c in (when if isinstance(when, list) else [when])]


@pytest.mark.parametrize("var", UNITS)
def test_the_unit_name_is_the_one_the_payload_ships(var):
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults[var] == UNITS[var][0]


@pytest.mark.parametrize("var", UNITS)
def test_each_unit_goes_through_the_shared_tasks_started_only_when_a_timer(var):
    """The boot unit runs at the next boot: starting it in a converge would run
    the release or the hold against a host that did not just restart."""
    suffix = UNITS[var][1]
    entry = _entries()["{{ " + var + " }}." + suffix]
    assert entry["start"] is (suffix == "timer")
    assert entry["what"] and entry["without"]


def test_the_decision_comes_from_the_shared_predicate_for_this_unit():
    task = _unit_tasks()[0]
    assert task["ansible.builtin.include_role"]["tasks_from"] == "_payload_expected"
    assert task["vars"]["_payload_paths"] == ["/etc/systemd/system/{{ _payload_unit.unit }}"]


def test_a_missing_unit_fails_a_converge_that_installs_the_payload():
    assert _when(_task("fail")) == ["catena_payload_missing | length > 0",
                                    "catena_payload_expected | bool"]


def test_a_payload_staged_out_of_band_defers_and_says_so():
    assert _when(_task("debug")) == ["catena_payload_missing | length > 0",
                                     "not (catena_payload_expected | bool)"]


def test_the_unit_is_enabled_only_once_it_is_on_the_host():
    """A unit whose files are absent cannot be enabled, so an ungated enable
    would turn the deferral into the failure it exists to avoid."""
    task = _task("systemd_service")
    unit = task["ansible.builtin.systemd_service"]
    assert unit["name"] == "{{ _payload_unit.unit }}"
    assert unit["enabled"] is True
    assert unit["state"] == "{{ 'started' if _payload_unit.start else omit }}"
    assert _when(task) == ["not ansible_check_mode",
                           "catena_payload_missing | length == 0"]


def test_the_unit_tasks_read_their_own_include():
    """The predicate's facts are overwritten by the next include, so another
    include between this one and the tasks reading it would answer for some
    other path."""
    rest = _unit_tasks()[1:]
    assert not [t for t in rest if "ansible.builtin.include_role" in t]
