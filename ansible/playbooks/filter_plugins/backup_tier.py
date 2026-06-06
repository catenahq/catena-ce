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


class FilterModule:
    def filters(self):
        return {
            "backup_tier_schedule": backup_tier_schedule,
            "backup_tier_retention": backup_tier_retention,
            "backup_tier_worm_oncalendar": backup_tier_worm_oncalendar,
        }
