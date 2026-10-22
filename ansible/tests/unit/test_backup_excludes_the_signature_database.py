"""The antivirus signature database stays out of the backups.

The ClamAV application keeps it in its clamav-db volume, which a stack named
<stack> holds as <stack>_clamav-db. freshclam re-downloads and rewrites it
several times a day, so a snapshot of it is cold-storage bloat that a restore
does not need.

Run: uv run pytest tests/unit/test_backup_excludes_the_signature_database.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

DEFAULTS = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"
)
VOLUMES = "/srv/catena/docker/volumes"


def _patterns() -> list[str]:
    return yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))["backup_exclude_patterns"]


def _excluded(path: str) -> bool:
    """restic's rule for a pattern with no leading slash: it matches the end of
    the path, `*` stands for part of one path component and `**` for any run.
    An excluded directory is not entered, so what is under one is excluded too."""
    parts = path.split("/")
    for end in range(2, len(parts) + 1):
        prefix = "/".join(parts[:end])
        for pattern in _patterns():
            regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
            if re.search(rf"(^|/){regex}$", prefix):
                return True
    return False


@pytest.mark.parametrize("stack", ["clamav", "antivirus", "scanner-2"])
def test_the_volume_and_what_is_in_it_are_excluded_whatever_the_stack_is_called(stack):
    volume = f"{VOLUMES}/{stack}_clamav-db"
    assert _excluded(volume)
    assert _excluded(f"{volume}/_data")
    assert _excluded(f"{volume}/_data/main.cvd")


@pytest.mark.parametrize("volume", [
    "catena-mailserver_mail-data", "catena-mailserver_mail-state",
    "catena-nextcloud_nc-data", "clamav_other-db", "catena-postgres-data",
])
def test_no_other_volume_is_swept_in(volume):
    assert not _excluded(f"{VOLUMES}/{volume}")
    assert not _excluded(f"{VOLUMES}/{volume}/_data/file")
