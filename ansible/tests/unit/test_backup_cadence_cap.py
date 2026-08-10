"""The Community backup cadence cap.

Community schedules at most one backup a week; daily and sub-daily cadence is
the licensed lane. catena-admin refuses a tighter schedule where the operator
types it, but /etc/catena/config.json is a file on a machine the client owns,
so this filter is the thing that actually holds. CV8 is a structural claim
about this repo, and a claim enforced only in a UI is not one.

The tests live beside the filter, so the tested copy is the shipped copy --
not a second copy imported from elsewhere that no playbook actually uses.

Run: uv run pytest tests/unit/test_backup_cadence_cap.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "backup_cadence_cap.py"
)
_spec = importlib.util.spec_from_file_location("backup_cadence_cap", _PLUGIN)
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

cap = B.backup_weekly_cap


@pytest.mark.parametrize("value", [
    "weekly", "monthly", "quarterly", "semiannually", "yearly", "annually",
    "WEEKLY", "Weekly",
])
def test_weekly_or_sparser_shorthands_pass(value: str) -> None:
    assert cap(value) == value


@pytest.mark.parametrize("value", [
    "Sun *-*-* 03:00:00",
    "sun *-*-* 03:00:00",
    "Saturday *-*-* 23:30:00",
    "Mon *-*-* 00:00:00",
])
def test_one_weekday_one_time_passes(value: str) -> None:
    assert cap(value) == value


@pytest.mark.parametrize("value", [
    "daily",
    "hourly",
    "*-*-* 03:00:00",             # every day
    "*-*-* *:00,15,30,45:00",     # every fifteen minutes
    "*-*-* 00,06,12,18:00:00",    # four times a day
])
def test_daily_and_sub_daily_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        cap(value)


@pytest.mark.parametrize("value", [
    "Sun,Wed *-*-* 03:00:00",   # two weekdays
    "Mon..Fri *-*-* 03:00:00",  # a range of weekdays
    "Sun *-*-* 03,15:00:00",    # one weekday, two times
    "Sun *-*-* 03..05:00:00",   # one weekday, an hour range
])
def test_expressions_that_fire_more_than_once_a_week_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        cap(value)


@pytest.mark.parametrize("value", [None, "", "   ", 42, [], {}])
def test_a_non_string_or_blank_is_refused(value) -> None:
    # A blank OnCalendar renders a timer that never fires. Passing it through
    # would read as "capped, fine" and produce a host with no backups.
    with pytest.raises(ValueError):
        cap(value)


def test_the_refusal_says_what_to_do_instead() -> None:
    # Read by someone who just had their input rejected and has no other
    # source for the rule.
    with pytest.raises(ValueError) as e:
        cap("daily")
    msg = str(e.value)
    assert "Sun *-*-* 03:00:00" in msg, msg
    assert "Catena Pro" in msg, msg


def test_the_tier_mapping_is_gone() -> None:
    # The three tiers mapped a name to a cadence and a retention ladder. The
    # cadence half rendered into no unit, so every tier ran at the same
    # hardcoded times while only retention varied. Cadence is a customizable
    # RPO set per lane now, and retention is five explicit fields.
    for gone in (
        "backup_tier_schedule",
        "backup_tier_retention",
        "backup_tier_worm_oncalendar",
    ):
        assert not hasattr(B, gone), f"{gone} survived the tier retirement"
    assert set(B.FilterModule().filters()) == {"backup_weekly_cap"}
