"""Ansible filter: map a backup tier name to its concrete schedule
+ retention policy + cold-mirror cadence.

Three named tiers ladder client RPO against price tier without
forcing every client onto the same schedule:

    realtime   15-min hot snapshots, 24h of hourly granularity
               (96 snapshots), daily cold mirror at 04:30. Highest
               cost; fits 15-min RPO promises on the most regulated
               or high-velocity clients.

    standard   hourly hot snapshots (the catena default since
               2026-05-17), 24h of hourly granularity, daily cold
               mirror. Default for new + existing clients.

    relaxed    6-hour hot snapshots, NO hourly retention (24h
               coverage comes from the 4 6-hour snapshots), weekly
               Sunday cold mirror. Lowest cost; suits low-velocity
               clients comfortable with a 6-hour RPO.

`backup_tier_schedule(tier)` returns the systemd OnCalendar string
for the hot backup unit. `backup_tier_retention(tier)` returns the
retention policy dict consumed by run-backup.sh's `restic forget
--keep-*` flags. `backup_tier_worm_oncalendar(tier)` returns the
OnCalendar for catena-restic-mirror.timer.

Invalid tiers raise ValueError so a converge fails loudly instead
of silently falling through to a default; matches the workflow
where BACKUP_TIER comes from inventory `.env`.

End-to-end coverage:
    automation/tests/unit/test_backup_tier_schedule.py
"""
from __future__ import annotations


_TIER_SPECS = {
    "realtime": {
        "schedule": "*-*-* *:00,15,30,45:00",
        "retention": {
            # 24 hours of 15-min snapshots = 96 entries. Restic's
            # content-defined chunking makes these near-free at the
            # pack-file level; the per-cycle cost is the
            # logical-dump quiesce + S3 PUTs.
            "keep_hourly": 96,
            "keep_daily": 14,
            "keep_weekly": 8,
            "keep_monthly": 6,
        },
        "worm_oncalendar": "*-*-* 04:30:00",
    },
    "standard": {
        "schedule": "hourly",
        "retention": {
            # 24h of hourly snapshots + the existing daily/weekly/
            # monthly tail. Matches the role's pre-Sprint-3.1 default;
            # explicit no-change for existing clients.
            "keep_hourly": 24,
            "keep_daily": 7,
            "keep_weekly": 4,
            "keep_monthly": 6,
        },
        "worm_oncalendar": "*-*-* 04:30:00",
    },
    "relaxed": {
        "schedule": "*-*-* 00,06,12,18:00:00",
        "retention": {
            # 6-hour snapshots make hourly retention meaningless;
            # the daily lane catches everything older than 24h. The
            # weekly/monthly tail is deeper to compensate for the
            # sparser daily lane (4 cycles per day vs 24).
            "keep_hourly": 0,
            "keep_daily": 14,
            "keep_weekly": 8,
            "keep_monthly": 6,
        },
        # Weekly cold mirror on Sunday at 04:30 -- the daily restic
        # snapshots roll up cheaply, and a weekly cold mirror keeps
        # cold-bucket egress costs proportional to client tier.
        "worm_oncalendar": "Sun *-*-* 04:30:00",
    },
}


VALID_TIERS = tuple(_TIER_SPECS.keys())


def _spec(tier):
    if not isinstance(tier, str):
        raise ValueError(
            f"backup_tier expects a string, got {type(tier).__name__}"
        )
    key = tier.strip().lower()
    spec = _TIER_SPECS.get(key)
    if spec is None:
        raise ValueError(
            f"backup_tier {tier!r} is not one of {VALID_TIERS!r}. "
            f"Set BACKUP_TIER in inventory `.env` to one of those values."
        )
    return spec


def backup_tier_schedule(tier):
    """systemd OnCalendar for the hot backup timer."""
    return _spec(tier)["schedule"]


def backup_tier_retention(tier):
    """restic forget --keep-* policy as a dict."""
    return dict(_spec(tier)["retention"])


def backup_tier_worm_oncalendar(tier):
    """systemd OnCalendar for the cold-tier mirror (catena-restic-mirror.timer)."""
    return _spec(tier)["worm_oncalendar"]


# Community cadence cap: the single CE timer (catena-backup.timer) is
# rate-limited to weekly or sparser. Daily and sub-daily cadence is the
# licensed lane (the EE catena-daily engine masks the CE timer and
# schedules backups itself). The cap is a hard converge-time gate, not
# documentation: an operator .env that tightens the cadence fails the
# play loudly.
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


class FilterModule:
    def filters(self):
        return {
            "backup_tier_schedule": backup_tier_schedule,
            "backup_tier_retention": backup_tier_retention,
            "backup_tier_worm_oncalendar": backup_tier_worm_oncalendar,
            "backup_weekly_cap": backup_weekly_cap,
        }
