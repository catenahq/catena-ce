"""Ansible filter: the database archives a snapshot carries, by basename.

The post-restore replay may only replay archives that travelled in the
snapshot being restored. Anything else under
{{ storage_mount_point }}/backup-staging/pg was on disk before the restore
ran -- a pre-seeded migration target holds an earlier snapshot's archives
until the final delta restore lands -- and replaying one of those over
freshly restored volumes overwrites newer data with older.

Membership is the only thing that answers this. No comparison of timestamps
does:

  - pg_dumpall writes an archive BEFORE the `restic backup` that captures
    it, so every archive is older than the snapshot it travels in;
  - a host takes many snapshots between two dumps, so an archive is
    routinely older than several of its own host's snapshots;
  - after a migration the newest archive was written on the machine the
    data came FROM, so it is older than every snapshot the receiving host
    has ever taken, and it is still the archive that must replay.

Input is `restic ls --json <snapshot> <dir>` -- one JSON object per line, a
snapshot header first and then a node per path. Output is the sorted list of
FILE basenames directly under dir. Unreadable lines are skipped rather than
fatal: restic has added fields to this stream before, and a restore must not
die on one it does not recognise.

The caller distinguishes "restic could not be read" from "the snapshot
carries nothing" by restic's exit code, because both arrive here as no
usable lines and only the first one may disable the guard.
"""
from __future__ import annotations

import json
import posixpath


def snapshot_dump_archives(ls_stdout, directory) -> list:
    """Sorted basenames of the files `restic ls --json` reported under
    `directory`.

    Returns [] for input that cannot be read, which the caller must not
    confuse with a snapshot that genuinely carries no archives; it gates on
    the command's rc before using this at all.
    """
    if isinstance(ls_stdout, bytes):
        ls_stdout = ls_stdout.decode("utf-8", "replace")
    if not isinstance(ls_stdout, str):
        return []
    wanted = posixpath.normpath(str(directory or "")) if directory else ""

    names = set()
    for line in ls_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            node = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(node, dict):
            continue
        # The header line describes the snapshot, not a path in it.
        if node.get("struct_type") == "snapshot":
            continue
        if node.get("type") != "file":
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        path = node.get("path")
        # Only files directly under the directory asked about: a nested
        # directory's contents are not archives this replay would pick up.
        if wanted and isinstance(path, str) and path:
            if posixpath.dirname(posixpath.normpath(path)) != wanted:
                continue
        names.add(name)
    return sorted(names)


class FilterModule:
    def filters(self):
        return {"snapshot_dump_archives": snapshot_dump_archives}
