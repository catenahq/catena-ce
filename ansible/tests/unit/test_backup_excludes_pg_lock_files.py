"""Postgres lock files must never reach a snapshot.

`postmaster.pid` records the PID, port and shared-memory key of a running
server. The infra Postgres volume is restored RAW -- deliberately, because
the store-derived superuser password makes a byte-for-byte restore correct
-- so anything sitting in that data dir comes back with it.

Two distinct failures, and the second is the one that was observed:

  a restored lock file is a STARTUP HAZARD. Postgres refuses to start when
  the PID in the lock file is live and belongs to another process, and a
  PID carried over from a different machine is arbitrary.

  it breaks the restore. `restic restore --delete` recreates the file and
  then sets its metadata; postgres clearing its own stale lock in between
  makes lchown fail on a path that no longer exists. restic counts that as
  an error and exits 1, so the entire restore reports failed over a file
  that should not have been in the snapshot:

      ignoring error for .../pgdata/postmaster.pid:
        lchown ...: no such file or directory
      Fatal: There were 1 errors

  Bench 2026-07-30T09-22-58-eff2, restore_version_skew_abort stage-4.

Run: uv run pytest tests/unit/test_backup_excludes_pg_lock_files.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

DEFAULTS = (
    Path(__file__).resolve().parents[2]
    / "roles" / "backup" / "defaults" / "main.yml"
)


def _patterns() -> list[str]:
    return yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))[
        "backup_exclude_patterns"]


@pytest.mark.parametrize("name", ["postmaster.pid", "postmaster.opts"])
def test_lock_file_is_excluded_recursively(name):
    patterns = _patterns()
    assert f"**/{name}" in patterns, (
        f"{name} must be excluded with `**`, not a fixed path: the infra "
        f"volume keeps it at /_data/pgdata/{name} and app images at "
        f"/_data/{name}, and restic's `*` does not cross slashes"
    )


def test_the_postgres_data_volume_itself_is_still_included():
    """The exclusion is scoped to runtime lock files. Excluding the volume
    would silently turn the infra database into something the restore
    cannot bring back, which is the opposite of the intent."""
    patterns = _patterns()
    for pattern in patterns:
        assert "catena-postgres-data" not in pattern, (
            f"{pattern!r} would drop the infra Postgres data volume; only "
            f"its lock files belong in this list"
        )
