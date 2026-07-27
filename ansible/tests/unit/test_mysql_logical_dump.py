"""MySQL/MariaDB apps get a logical dump, like Postgres ones do.

Seven catalog templates ship a MariaDB or MySQL: erpnext, wordpress, kimai,
espocrm, mautic, invoiceninja, easyappointments. Their data volumes are in
the restic set like every other volume, but until this existed the raw
volume was the ONLY copy -- no logical dump to check it against, and no way
to restore one database without restoring the whole volume. A raw copy of a
running InnoDB directory is crash-consistent at best.

Run: uv run pytest tests/unit/test_mysql_logical_dump.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
RUN_BACKUP = ANSIBLE / "scripts" / "run-backup.sh"
BACKUP_DEFAULTS = ANSIBLE / "roles" / "backup" / "defaults" / "main.yml"

BODY = RUN_BACKUP.read_text(encoding="utf-8")

# Comment lines name the anti-patterns in order to rule them out, so the
# "must not appear" assertions read code only.
CODE = "\n".join(
    line for line in BODY.splitlines() if not line.lstrip().startswith("#")
)


def test_mysql_and_mariadb_containers_are_dumped():
    assert "tolower($2) ~ /mariadb|mysql/" in BODY, (
        "the container scan must match both image families"
    )


def test_the_dumps_land_outside_the_postgres_directory():
    # catena-recovery's replay feeds every *.sql.gz under backup-staging/pg to
    # psql. A MariaDB dump landing there would be fed to the wrong engine on
    # every recovery.
    assert 'MYSQL_DIR="${BACKUP_STAGING_DIR}/mysql"' in BODY
    assert '${MYSQL_DIR}/${name}-${ts}.sql.gz' in BODY


def test_the_root_password_never_crosses_the_host_command_line():
    # The container already holds its own root password, so reading it inside
    # the container keeps it out of this host's process table -- which
    # `docker exec -e PW=...` would expose to every user on the box.
    assert "MARIADB_ROOT_PASSWORD" in CODE
    assert "docker exec -e" not in CODE, (
        "passing the password through docker exec -e puts it in `ps`"
    )
    for leak in ("-p$", "--password="):
        assert leak not in CODE, f"{leak!r} would put the password in argv"


def test_the_dump_is_transactionally_consistent_and_complete():
    # --single-transaction gives InnoDB a consistent snapshot without locking
    # the application out. The rest is the part of a schema that is not
    # tables, whose absence only shows up when someone restores and a report
    # stops working.
    for flag in ("--single-transaction", "--routines", "--triggers", "--events"):
        assert flag in BODY, flag


def test_it_prefers_the_mariadb_binary_and_falls_back():
    # MariaDB 11 renamed the client binaries and ships mysqldump only as a
    # compatibility symlink that some images drop.
    assert "command -v mariadb-dump" in BODY
    assert "command -v mysqldump" in BODY


def test_a_failed_dump_is_fatal():
    # Same reason a pg_dumpall failure is: the dump failing means the engine
    # did not answer, and snapshotting its volume in that state while calling
    # the run a success is how a broken database becomes a broken backup.
    assert 'mysql_failures="${mysql_failures} ${c}"' in BODY
    assert "FATAL: mysqldump failed for:" in BODY


def test_the_staging_directory_rides_the_backup():
    defaults = yaml.safe_load(BACKUP_DEFAULTS.read_text())
    paths = defaults["backup_paths"]
    assert any("backup-staging" in p for p in paths), (
        "the mysql dumps live under backup-staging, which must be in the set"
    )
