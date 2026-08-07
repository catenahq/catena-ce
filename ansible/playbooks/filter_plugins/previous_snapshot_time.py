"""Ansible filter: the time of the backup taken BEFORE a given snapshot.

That time is the staleness floor for the pg_dumpall archives a restore
leaves under {{ storage_mount_point }}/backup-staging/pg. The post-restore
replay refuses an archive older than the floor, because an archive older
than the previous backup did not come out of the snapshot being restored --
it is a leftover from an earlier pass, and replaying it over freshly
restored volumes overwrites newer data with older.

The snapshot's OWN time is not a usable floor. pg_dumpall writes the
archives before `restic backup` runs, so every archive a snapshot carries
predates the snapshot, and restic restores their original mtimes. A floor
set to the snapshot's own time refuses every legitimate archive.

Input is `restic snapshots --json` (the whole repository) plus the id of the
snapshot being restored. Output is an RFC3339 string, or "" when the
repository holds no earlier snapshot -- a first backup has no earlier pass
for a leftover to have come from, so there is nothing to guard against.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _parse_time(value):
    """restic writes RFC3339 with a variable-width fractional second."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Python's fromisoformat rejects "Z" before 3.11 and rejects more than 6
    # fractional digits in every version; restic emits 9.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                tail = tail[len(digits):]
                break
        else:
            tail = ""
        text = head + "." + digits[:6] + tail
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Always aware: comparing a naive datetime with an aware one raises, and a
    # single naive record would take the whole restore down with a TypeError.
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


def previous_snapshot_time(snapshots_json, snapshot_id) -> str:
    """RFC3339 time of the newest snapshot strictly older than `snapshot_id`.

    Returns "" when there is none, when the id is not in the list, or when
    the input cannot be read. An empty answer disables the guard, which is
    why the caller asserts the list parsed rather than trusting this to be
    the only check.
    """
    if isinstance(snapshots_json, (str, bytes)):
        try:
            records = json.loads(snapshots_json or "[]")
        except (ValueError, TypeError):
            return ""
    else:
        records = snapshots_json
    if not isinstance(records, list):
        return ""

    wanted = (snapshot_id or "").strip()
    # "latest" is what restore_snapshot defaults to, and it names the newest
    # snapshot rather than any id in the list.
    newest_wins = wanted in ("", "latest")

    target = None
    parsed = []
    for r in records:
        if not isinstance(r, dict):
            continue
        when = _parse_time(r.get("time"))
        if when is None:
            continue
        parsed.append(when)
        if newest_wins:
            continue
        ids = [str(r.get("short_id") or ""), str(r.get("id") or "")]
        # A full id matches the short id it was truncated from, which is what
        # an operator pasting either form into restore_snapshot produces.
        if wanted in ids or any(i.startswith(wanted) for i in ids if i):
            if target is None or when > target:
                target = when
    if newest_wins and parsed:
        target = max(parsed)
    if target is None:
        return ""

    older = [t for t in parsed if t < target]
    if not older:
        return ""
    return max(older).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FilterModule:
    def filters(self):
        return {"previous_snapshot_time": previous_snapshot_time}
