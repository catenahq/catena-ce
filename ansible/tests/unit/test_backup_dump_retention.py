"""Staging keeps the newest N dumps per container, not the last N days.

The rule was `find -mtime +3`, which deletes by AGE. It never matched the
comment beside it ("keep last 3 dumps per container"), and on a host backing
up more often than daily -- the licensed lane does -- it deletes nothing at
all. Bench run 5534 measured the consequence on a source four and a half hours
into the run: five catena-postgres dumps and four nextcloud-db-1, every one of
them inside every snapshot restic took.

That is not only staging bloat. `restic restore --delete` makes a restored
host match the snapshot, so a migration target inherits the whole pile, and
every archive older than the volumes beside it is something the replay could
put on top of newer data. migrate_preseed_no_split_brain asserts the sweep
from the other end, on a real host.

These tests drive the real shipped run-backup.sh with `docker` and `restic`
stubbed, seed staging with archives that are all NEW on disk, and read back
what survived. Seeding them new is the point: an age-based rule passes a
count-based assertion only by accident, and here it cannot -- nothing is three
days old.

Run: uv run pytest tests/unit/test_backup_dump_retention.py
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]
RUN_BACKUP = ANSIBLE / "scripts" / "run-backup.sh"


def _exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run(tmp_path: Path, *, seed: dict[str, list[str]], keep: int | None = None,
         dump_rc: int = 0) -> tuple[subprocess.CompletedProcess, Path]:
    """Drive the real script over a pre-seeded staging directory.

    seed     subdirectory ("pg"/"mysql") -> archive basenames to create
    keep     BACKUP_DUMP_KEEP; None leaves it at the script's default
    dump_rc  exit code of the stubbed pg_dumpall
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)

    for sub, names in seed.items():
        d = staging / sub
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_text("fake archive\n")

    _exe(bin_dir / "docker", f"""#!/bin/sh
case "$1" in
  ps) printf 'catena-postgres.1.abc\tpostgres:18\\n' ;;
  exec)
    shift
    c="$1"; shift
    case "$1" in
      sh)         printf 'postgres' ;;
      psql)       printf '1024\\n' ;;
      pg_dumpall) printf -- '-- fake dump\\n'; exit {dump_rc} ;;
    esac
    ;;
esac
exit 0
""")
    _exe(bin_dir / "restic", "#!/bin/sh\nexit 0\n")
    _exe(bin_dir / "gzip", "#!/bin/sh\ncat\n")

    password = tmp_path / "restic.pass"
    password.write_text("hunter2\n")

    env_lines = [
        f"CATENA_CONFIG_STORE={tmp_path}/absent-config.json",
        f"BACKUP_RETENTION_ENV={tmp_path}/absent-retention.env",
        "RESTIC_REPOSITORY=s3:https://example.invalid/bucket",
        f"RESTIC_PASSWORD_FILE={password}",
        "AWS_ACCESS_KEY_ID=key",
        "AWS_SECRET_ACCESS_KEY=secret",
        "BACKUP_HEALTHCHECK_URL=",
        "BACKUP_HEALTHCHECK_ATTEMPTED_URL=",
        "BACKUP_HEALTHCHECK_URL_CLIENT=",
        "BACKUP_HEALTHCHECK_URL_OPERATOR=",
        "BACKUP_PG_DUMP_USER=postgres",
        f"BACKUP_STAGING_DIR={staging}",
        "BACKUP_DISK_PREFLIGHT_SCRIPT=",
        "BACKUP_PREFLIGHT_MIN_BYTES=0",
        f"BACKUP_PATHS_FILE={tmp_path}/absent-paths",
        f"BACKUP_EXCLUDE_FILE={tmp_path}/absent-excludes",
        f"BACKUP_LAST_SUCCESS_STAMP={tmp_path}/last-success",
        "BACKUP_STATS_FILE=",
        "BACKUP_COVERAGE_SCRIPT=",
        "BACKUP_SNAPSHOT_LIST_SCRIPT=",
        "BACKUP_RESTIC_CHECK_SCRIPT=",
    ]
    if keep is not None:
        env_lines.append(f"BACKUP_DUMP_KEEP={keep}")

    env_file = tmp_path / "backup.env"
    env_file.write_text("\n".join(env_lines) + "\n")

    proc = subprocess.run(
        ["/bin/sh", str(RUN_BACKUP), str(env_file)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    return proc, staging


def _archives(staging: Path, sub: str) -> list[str]:
    d = staging / sub
    return sorted(p.name for p in d.glob("*.sql.gz"))


OLD_PG = [
    "catena-postgres-20260802T231554Z.sql.gz",
    "catena-postgres-20260803T002343Z.sql.gz",
    "catena-postgres-20260803T030955Z.sql.gz",
    "catena-postgres-20260803T033542Z.sql.gz",
    "nextcloud-db-1-20260803T002344Z.sql.gz",
    "nextcloud-db-1-20260803T030955Z.sql.gz",
]


def test_only_the_newest_survive_and_the_count_is_per_container(tmp_path):
    # The exact pile bench run 5534 measured, plus whatever this pass writes.
    proc, staging = _run(tmp_path, seed={"pg": OLD_PG}, keep=2)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    left = _archives(staging, "pg")
    pg = [n for n in left if n.startswith("catena-postgres-")]
    nc = [n for n in left if n.startswith("nextcloud-db-1-")]

    # Per container, not per directory: nextcloud keeps its own two even
    # though catena-postgres has more and newer ones.
    assert len(nc) == 2, left
    assert nc == sorted(nc)[-2:], left

    # catena-postgres gets a fresh dump this pass, so its two are the new one
    # plus the newest seeded one. The 2315 and 0023 stamps must be gone.
    assert len(pg) == 2, left
    assert "catena-postgres-20260802T231554Z.sql.gz" not in pg
    assert "catena-postgres-20260803T002343Z.sql.gz" not in pg


def test_the_new_dump_is_inside_the_kept_set_not_extra(tmp_path):
    # Pruning BEFORE the dumps would keep N old archives and then add one, so
    # the snapshot restic takes would always carry N+1. The whole point is
    # that the snapshot carries exactly N.
    proc, staging = _run(tmp_path, seed={"pg": OLD_PG}, keep=3)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    pg = [n for n in _archives(staging, "pg") if n.startswith("catena-postgres-")]
    assert len(pg) == 3, pg
    # The newest is the one this pass just wrote, not a seeded stamp.
    assert pg[-1] not in OLD_PG, pg


def test_a_failed_dump_prunes_nothing(tmp_path):
    # A pass that could not read the database must not also delete the last
    # archives that CAN be replayed. The run aborts before the prune.
    proc, staging = _run(tmp_path, seed={"pg": OLD_PG}, keep=1, dump_rc=1)
    assert proc.returncode == 2, proc.stdout + proc.stderr

    left = _archives(staging, "pg")
    for name in OLD_PG:
        assert name in left, f"{name} was deleted by a failed pass: {left}"


def test_mysql_staging_is_pruned_by_the_same_rule(tmp_path):
    # Same accumulation, same hazard: catena-recovery replays pg/, but a
    # mysql/ pile is still restored onto every target.
    mysql = [
        "erpnext-db-1-20260802T231554Z.sql.gz",
        "erpnext-db-1-20260803T002343Z.sql.gz",
        "erpnext-db-1-20260803T030955Z.sql.gz",
    ]
    proc, staging = _run(tmp_path, seed={"pg": [], "mysql": mysql}, keep=1)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    left = _archives(staging, "mysql")
    assert left == ["erpnext-db-1-20260803T030955Z.sql.gz"], left
