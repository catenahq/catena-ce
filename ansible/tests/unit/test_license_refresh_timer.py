"""reconcile/roles/host_maintenance enables the payload's licence-check and
error-report timers.

catena-license, catena-error-report and their units ship in the catena-admin
payload; this role only enables each timer, on every host. Whether an absent
timer is a defect depends on whether THIS converge installed the payload: one
that did fails, one whose payload is staged out of band defers and says so, and
nothing touches systemd until the timer is on the host.

Run: uv run pytest tests/unit/test_license_refresh_timer.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[2] / "reconcile" / "roles" / "host_maintenance"
TASKS = ROLE / "tasks" / "main.yml"

# Each timer's task-name prefix, the default naming its unit, and the unit the
# payload ships.
TIMERS = {
    "Licence check": ("license_refresh_systemd_unit_name", "catena-license-refresh"),
    "Error reports": ("error_report_systemd_unit_name", "catena-error-report"),
}


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


def _timer(prefix: str) -> str:
    return "/etc/systemd/system/{{ " + TIMERS[prefix][0] + " }}.timer"


@pytest.mark.parametrize("prefix", TIMERS)
def test_the_unit_name_is_the_one_the_payload_ships(prefix):
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    var, unit = TIMERS[prefix]
    assert defaults[var] == unit


@pytest.mark.parametrize("prefix", TIMERS)
def test_the_decision_comes_from_the_shared_predicate_for_this_timer(prefix):
    task = _task(f"{prefix}: is the payload-shipped timer expected")
    assert task["ansible.builtin.include_role"]["tasks_from"] == "_payload_expected"
    assert task["vars"]["_payload_paths"] == [_timer(prefix)]


@pytest.mark.parametrize("prefix", TIMERS)
def test_a_missing_timer_fails_a_converge_that_installs_the_payload(prefix):
    task = _task(f"{prefix}: the timer is missing")
    assert "ansible.builtin.fail" in task
    assert _when(task) == ["catena_payload_missing | length > 0",
                           "catena_payload_expected | bool"]


@pytest.mark.parametrize("prefix", TIMERS)
def test_a_payload_staged_out_of_band_defers_and_says_so(prefix):
    task = _task(f"{prefix}: timer staged out of band")
    assert "ansible.builtin.debug" in task
    assert _when(task) == ["catena_payload_missing | length > 0",
                           "not (catena_payload_expected | bool)"]


@pytest.mark.parametrize("prefix", TIMERS)
def test_the_timer_is_enabled_only_once_it_is_on_the_host(prefix):
    """A .timer whose files are absent cannot be enabled, so an ungated enable
    would turn the deferral into the failure it exists to avoid."""
    task = _task(f"{prefix}: enable + start timer")
    unit = task["ansible.builtin.systemd_service"]
    assert unit["name"] == "{{ " + TIMERS[prefix][0] + " }}.timer"
    assert unit["enabled"] is True and unit["state"] == "started"
    assert _when(task) == ["not ansible_check_mode",
                           "catena_payload_missing | length == 0"]


@pytest.mark.parametrize("prefix", TIMERS)
def test_the_timer_tasks_read_their_own_include(prefix):
    """The predicate's facts are overwritten by the next include, so another
    include between this one and the tasks reading it would answer for some
    other path."""
    start = _index(f"{prefix}: is the payload-shipped timer expected")
    end = _index(f"{prefix}: enable + start timer")
    between = _tasks()[start + 1:end + 1]
    assert start < end
    assert not [t for t in between if "ansible.builtin.include_role" in t]
