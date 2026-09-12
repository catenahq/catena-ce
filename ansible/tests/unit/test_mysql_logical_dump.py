"""MySQL/MariaDB dumps ride the backup set.

Seven catalog templates ship a MariaDB or MySQL: erpnext, wordpress, kimai,
espocrm, mautic, invoiceninja, easyappointments. Their data volumes are in the
restic set like every other volume, and without a logical dump that raw volume
is the ONLY copy -- nothing to check it against, and no way to restore one
database without restoring the whole volume. A raw copy of a running InnoDB
directory is crash-consistent at best.

The assertions about the DUMP ITSELF (which containers are matched, where the
archives land, that the root password never reaches argv, the consistency
flags, the binary fallback, that a failure is fatal) live beside the wrapper, in
catena-admin payload/lanes/backup_run_mysql_test.go. This repo owns the other
half: the staging directory has to be in backup_paths, or the dumps are written
and never captured.

Run: uv run pytest tests/unit/test_mysql_logical_dump.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BACKUP_DEFAULTS = ANSIBLE / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"


def test_the_staging_directory_rides_the_backup():
    defaults = yaml.safe_load(BACKUP_DEFAULTS.read_text())
    paths = defaults["backup_paths"]
    assert any("backup-staging" in p for p in paths), (
        "the mysql dumps live under backup-staging, which must be in the set"
    )
