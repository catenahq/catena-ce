"""Ansible filter: the Community backup cadence cap.

Community schedules at most one backup a week. Daily and sub-daily cadence,
and the catena-daily orchestrator chain that drives it, are the licensed lane.

This is the BOUNDARY, not the affordance. catena-admin refuses a tighter
schedule at the moment an operator types it, which is where the message
belongs -- but /etc/catena/config.json is a file on a machine the client owns
and can edit, so the converge has to be the thing that actually holds. CV8
("scheduled work is default-deny") is a structural claim about this repo, and
a claim enforced only in a UI is not one.

Raising ValueError fails the play loudly, which is the intent: an operator
`.env` or an edited config store that tightens the cadence on an unlicensed
host is a licensing question, not a warning.

This file used to also map three tier names to a cadence and a retention
ladder. The tiers are gone: their cadence half was computed and rendered into
no unit at all, so every tier ran at the same hardcoded times while only
retention varied -- a `realtime` host was configured to keep 96 hourly
snapshots while producing one snapshot a day. Cadence is now a customizable
RPO set per lane in the panel, and retention is five explicit fields.

End-to-end coverage:
    ops automation/tests/unit/test_backup_cadence_cap.py
"""
from __future__ import annotations

import re


_WEEKLY_OR_SPARSER_LITERALS = frozenset({
    "weekly", "monthly", "quarterly", "semiannually", "yearly", "annually",
})

_DAY_OF_WEEK = frozenset({
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
})

_CAP_HELP = (
    "Community backup cadence is capped at weekly. Allowed forms: the "
    "systemd shorthands ('weekly', 'monthly', 'quarterly', "
    "'semiannually', 'yearly') or a single day-of-week expression with "
    "one time point (e.g. 'Sun *-*-* 03:00:00'). Daily and sub-daily "
    "backups are a Catena Pro feature."
)


def backup_weekly_cap(oncalendar):
    """Validate a systemd OnCalendar value fires at most weekly.

    Returns the value unchanged when it is a weekly-or-sparser
    shorthand or a single-day-of-week expression with a single time
    point; raises ValueError for anything tighter (daily, hourly,
    date-only expressions, day lists/ranges, multiple time points)."""
    if not isinstance(oncalendar, str) or not oncalendar.strip():
        raise ValueError(f"OnCalendar must be a non-empty string. {_CAP_HELP}")
    value = oncalendar.strip()
    if value.lower() in _WEEKLY_OR_SPARSER_LITERALS:
        return oncalendar
    first, _, rest = value.partition(" ")
    day = first.lower()
    # 'Sun,Mon ...' or 'Mon..Fri ...' fire more than weekly.
    if "," in day or ".." in day:
        raise ValueError(
            f"OnCalendar {oncalendar!r} lists multiple weekdays. {_CAP_HELP}"
        )
    if day not in _DAY_OF_WEEK:
        raise ValueError(
            f"OnCalendar {oncalendar!r} is not weekly-or-sparser. {_CAP_HELP}"
        )
    # A single weekday with multiple time points ('Sun *-*-* 03,15:00:00')
    # or an hour range still fires more than once that day.
    if "," in rest or ".." in rest:
        raise ValueError(
            f"OnCalendar {oncalendar!r} has multiple time points. {_CAP_HELP}"
        )
    return oncalendar


_MONTHLY_OR_SPARSER_LITERALS = frozenset({
    "monthly", "quarterly", "semiannually", "yearly", "annually",
})

_MONTHLY_CAP_HELP = (
    "Community applies container updates at most once a month. Allowed "
    "forms: the systemd shorthands ('monthly', 'quarterly', "
    "'semiannually', 'yearly') or one day of the month with one time "
    "point (e.g. '*-*-01 04:00:00'). More frequent unattended updates "
    "are a Catena Pro feature."
)

_DAY_OF_MONTH = re.compile(r"^\*-\*-\d{1,2}$")


def community_monthly_cap(oncalendar):
    """Validate a systemd OnCalendar value fires at most monthly.

    A DIFFERENT question from the weekly backup cap, which is why it is a
    separate rule rather than a reuse. Weekly is a capability boundary:
    Community ships one weekly backup timer, so a tighter interval has no timer
    to fire it. Monthly is a blast-radius decision -- how often a host nobody is
    watching is allowed to change its own images.

    Deliberately narrow: systemd's grammar can express a monthly cadence many
    ways, and a validator that tried to decide the general case would
    eventually accept something that fires daily."""
    if not isinstance(oncalendar, str) or not oncalendar.strip():
        raise ValueError(f"OnCalendar must be a non-empty string. {_MONTHLY_CAP_HELP}")
    value = oncalendar.strip()
    if value.lower() in _MONTHLY_OR_SPARSER_LITERALS:
        return oncalendar
    first, _, rest = value.partition(" ")
    if not _DAY_OF_MONTH.match(first):
        raise ValueError(
            f"OnCalendar {oncalendar!r} is more frequent than monthly. "
            f"{_MONTHLY_CAP_HELP}"
        )
    if "," in rest or ".." in rest or "/" in rest:
        raise ValueError(
            f"OnCalendar {oncalendar!r} has multiple time points. "
            f"{_MONTHLY_CAP_HELP}"
        )
    return oncalendar


class FilterModule:
    def filters(self):
        return {
            "backup_weekly_cap": backup_weekly_cap,
            "community_monthly_cap": community_monthly_cap,
        }
