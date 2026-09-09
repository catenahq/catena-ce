"""The Community ceiling on unattended container updates.

The panel refuses a tighter cadence where the mistake is made; this is the
boundary, because /etc/catena/config.json is a file on a machine the client
owns and can edit. A limit enforced only in a UI is not one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANSIBLE / "playbooks" / "filter_plugins"))

from backup_cadence_cap import community_monthly_cap  # noqa: E402

HOST_TASKS = ANSIBLE / "roles" / "catena_admin_host" / "tasks" / "main.yml"


@pytest.mark.parametrize("expr", [
    "monthly", "quarterly", "semiannually", "yearly", "annually",
    "*-*-01 04:00:00", "*-*-15 22:30:00",
])
def test_monthly_or_sparser_is_accepted(expr):
    assert community_monthly_cap(expr) == expr


@pytest.mark.parametrize("expr", [
    "daily", "weekly", "hourly",
    "*-*-* 04:00:00",            # every day
    "Sun *-*-* 03:00:00",        # weekly
    "*-*-01,15 04:00:00",        # twice a month
    "*-*-01 04,16:00:00",        # twice that day
    "*-*-01 00/6:00:00",         # every six hours that day
])
def test_anything_tighter_is_refused(expr):
    with pytest.raises(ValueError) as e:
        community_monthly_cap(expr)
    assert "*-*-01 04:00:00" in str(e.value), (
        "the refusal must name a form that IS allowed, or the operator is left "
        "guessing at systemd calendar grammar")


def test_an_empty_expression_is_refused_rather_than_defaulted():
    for bad in ("", "   ", None, 3):
        with pytest.raises(ValueError):
            community_monthly_cap(bad)


def test_the_weekly_backup_cap_is_still_its_own_rule():
    """Reusing one cap for both would mean either a monthly backup ceiling or a
    weekly update one, and neither is the decision that was made."""
    from backup_cadence_cap import backup_weekly_cap
    assert backup_weekly_cap("Sun *-*-* 03:00:00")
    with pytest.raises(ValueError):
        community_monthly_cap("Sun *-*-* 03:00:00")


def test_the_converge_asserts_the_cap_against_the_stored_value():
    """The panel is the affordance; the converge is the boundary. Asserting
    against role defaults instead of the store would check a value the client
    cannot change and miss the one they can."""
    tasks = yaml.safe_load(HOST_TASKS.read_text())
    assertion = next(
        (t for t in tasks
         if "community_monthly_cap" in str(t.get("ansible.builtin.assert", {}))),
        None,
    )
    assert assertion is not None, "the converge no longer asserts the cap"
    conditions = str(assertion.get("when", ""))
    assert "enabled" in conditions, (
        "a stored expression on a DISABLED lane is not doing anything; failing "
        "the converge over it would block hosts that never turned it on")
    assert "catena_license" in conditions, "a licensed host is uncapped"
    # Every monthly-capped lane in catena-admin payload/engines/schedule
    # (schedule.CommunityCap == CapMonthly). A new key without an entry here is
    # a hole in the boundary: the panel would refuse the cadence and the file
    # the client can edit would not.
    capped = set(assertion.get("loop", []))
    assert capped == {"container_updates", "panel_updates"}, (
        f"the assertion caps {sorted(capped)}; that is not the monthly-capped "
        "lane set")
    assert "[item]" in str(assertion.get("vars", "")), (
        "the assertion reads a fixed lane rather than the one it is looping "
        "over, so every iteration checks the same key")
