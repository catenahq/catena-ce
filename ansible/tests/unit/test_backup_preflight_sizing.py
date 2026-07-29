"""The backup disk preflight must size itself from the live databases.

BACKUP_PREFLIGHT_MIN_BYTES was a constant sized for "an SMB-stack VPS". A
tenant with a large Postgres passed the check and then filled the disk
mid-dump, caught only by the fatal-pg_dumpall path -- after the backup window
was burned. run-backup.sh now sums pg_database_size across every running
postgres container and requires that instead whenever it exceeds the floor.

These tests drive the real shipped run-backup.sh with `docker` and `restic`
stubbed on PATH, and with BACKUP_DISK_PREFLIGHT_SCRIPT pointed at a recorder
that writes the argv it was handed. That argv IS the derived requirement, so
the assertions read the number the script actually decided on rather than a
re-implementation of the arithmetic.

The recorder's exit code is the run's fate: exit 0 and the script proceeds to
the dumps and finishes clean, exit 1 and the run aborts exactly where a real
short-on-space host would. Both paths are asserted.

Run: uv run pytest tests/unit/test_backup_preflight_sizing.py
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]
RUN_BACKUP = ANSIBLE / "scripts" / "run-backup.sh"

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


def _exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Run:
    """One run-backup.sh invocation and everything it left behind."""

    def __init__(self, proc: subprocess.CompletedProcess, root: Path):
        self.proc = proc
        self.root = root

    @property
    def rc(self) -> int:
        return self.proc.returncode

    @property
    def out(self) -> str:
        return self.proc.stdout + self.proc.stderr

    @property
    def preflight_argv(self) -> list[str] | None:
        """argv the disk preflight was called with, or None if never called."""
        rec = self.root / "preflight.argv"
        if not rec.exists():
            return None
        return rec.read_text().rstrip("\n").split("\n")

    @property
    def required_bytes(self) -> int:
        argv = self.preflight_argv
        assert argv is not None, "the disk preflight was never invoked"
        return int(argv[1])

    @property
    def docker_calls(self) -> list[str]:
        log = self.root / "docker.log"
        return log.read_text().splitlines() if log.exists() else []


def _run(
    tmp_path: Path,
    *,
    containers: list[tuple[str, str]] | None = None,
    sizes: dict[str, str] | None = None,
    users: dict[str, str] | None = None,
    floor: int | None = 0,
    db_pct: str | None = None,
    preflight_rc: int = 0,
) -> Run:
    """Drive the real script.

    containers  (name, image) rows the fake `docker ps` reports
    sizes       name -> what `psql` prints; a name absent from this dict is a
                container that refuses the query (stub exits non-zero)
    users       name -> POSTGRES_USER in the container env; absent means unset,
                so the script falls back to BACKUP_PG_DUMP_USER
    floor       BACKUP_PREFLIGHT_MIN_BYTES; None leaves the key out entirely
    """
    containers = containers if containers is not None else []
    sizes = sizes or {}
    users = users or {}

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    data = tmp_path / "docker-data"
    data.mkdir(exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)

    (data / "ps").write_text(
        "".join(f"{name}\t{image}\n" for name, image in containers)
    )
    for name, size in sizes.items():
        (data / f"{name}.size").write_text(size + "\n")
    for name, user in users.items():
        (data / f"{name}.user").write_text(user)

    _exe(bin_dir / "docker", f"""#!/bin/sh
printf '%s\\n' "$*" >> {tmp_path}/docker.log
data={data}
case "$1" in
  ps) cat "$data/ps" ;;
  exec)
    shift
    c="$1"; shift
    case "$1" in
      sh)         [ -f "$data/$c.user" ] && cat "$data/$c.user" ;;
      psql)       [ -f "$data/$c.size" ] || exit 1; cat "$data/$c.size" ;;
      pg_dumpall) printf -- '-- fake dump for %s\\n' "$c" ;;
    esac
    ;;
esac
exit 0
""")

    # restic is not under test here: every call succeeds so the run reaches
    # its natural end and the preflight decision is the only variable.
    _exe(bin_dir / "restic", "#!/bin/sh\nexit 0\n")

    _exe(bin_dir / "catena-disk-preflight", f"""#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > {tmp_path}/preflight.argv
exit {preflight_rc}
""")

    password = tmp_path / "restic.pass"
    password.write_text("hunter2\n")

    env_lines = [
        f"CATENA_CONFIG_STORE={tmp_path}/absent-config.json",
        f"BACKUP_RETENTION_ENV={tmp_path}/absent-retention.env",
        "RESTIC_REPOSITORY=s3:https://example.invalid/bucket",
        f"RESTIC_PASSWORD_FILE={password}",
        "AWS_ACCESS_KEY_ID=key",
        "AWS_SECRET_ACCESS_KEY=secret",
        # Both ping lanes blank: no curl stub needed, and a ping is not what
        # these tests are about.
        "BACKUP_HEALTHCHECK_URL=",
        "BACKUP_HEALTHCHECK_ATTEMPTED_URL=",
        "BACKUP_HEALTHCHECK_URL_CLIENT=",
        "BACKUP_HEALTHCHECK_URL_OPERATOR=",
        "BACKUP_PG_DUMP_USER=postgres",
        f"BACKUP_STAGING_DIR={staging}",
        f"BACKUP_DISK_PREFLIGHT_SCRIPT={bin_dir}/catena-disk-preflight",
        f"BACKUP_PATHS_FILE={tmp_path}/absent-paths",
        f"BACKUP_EXCLUDE_FILE={tmp_path}/absent-excludes",
        f"BACKUP_LAST_SUCCESS_STAMP={tmp_path}/last-success",
        "BACKUP_STATS_FILE=",
        "BACKUP_COVERAGE_SCRIPT=",
        "BACKUP_SNAPSHOT_LIST_SCRIPT=",
    ]
    if floor is not None:
        env_lines.append(f"BACKUP_PREFLIGHT_MIN_BYTES={floor}")
    if db_pct is not None:
        env_lines.append(f"BACKUP_PREFLIGHT_DB_PCT={db_pct}")

    env_file = tmp_path / "backup.env"
    env_file.write_text("\n".join(env_lines) + "\n")

    proc = subprocess.run(
        ["/bin/sh", str(RUN_BACKUP), str(env_file)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    return Run(proc, tmp_path)


def test_the_requirement_is_derived_from_the_live_databases(tmp_path: Path):
    run = _run(
        tmp_path,
        containers=[("catena-postgres.1.abc", "postgres:18"),
                    ("nextcloud-db-1", "postgres:16-alpine")],
        sizes={"catena-postgres.1.abc": str(3 * GIB),
               "nextcloud-db-1": str(1 * GIB)},
        floor=1 * GIB,
    )
    assert run.rc == 0, run.out
    assert run.required_bytes == 4 * GIB
    assert run.preflight_argv[0] == str(tmp_path / "staging")
    assert "backup preflight" in run.preflight_argv[2]


def test_the_static_floor_still_wins_when_the_databases_are_small(tmp_path: Path):
    """A small tenant must not get a SMALLER requirement than before: the
    derived number replaces the floor only when it is larger."""
    run = _run(
        tmp_path,
        containers=[("nextcloud-db-1", "postgres:16")],
        sizes={"nextcloud-db-1": str(100 * MIB)},
        floor=2 * GIB,
    )
    assert run.rc == 0, run.out
    assert run.required_bytes == 2 * GIB
    assert "preflight sizing: databases total" not in run.out


def test_db_pct_scales_the_requirement(tmp_path: Path):
    run = _run(
        tmp_path,
        containers=[("db-1", "postgres:18")],
        sizes={"db-1": str(2 * GIB)},
        floor=0,
        db_pct="150",
    )
    assert run.rc == 0, run.out
    assert run.required_bytes == 3 * GIB


def test_a_container_that_refuses_the_query_is_skipped_not_fatal(tmp_path: Path):
    """Fail-open: a preflight that blocks on its own instrumentation is worse
    than the gap it closes. The silent container is logged and not counted;
    the ones that answered still raise the requirement."""
    run = _run(
        tmp_path,
        containers=[("good-db", "postgres:18"), ("mute-db", "postgres:18")],
        sizes={"good-db": str(5 * GIB)},  # mute-db absent -> stub exits 1
        floor=1 * GIB,
    )
    assert run.rc == 0, run.out
    assert run.required_bytes == 5 * GIB
    assert "mute-db did not answer the database-size query" in run.out


def test_a_non_numeric_answer_does_not_poison_the_requirement(tmp_path: Path):
    """psql printing an error onto stdout must not be arithmetic input --
    `$((x + FATAL))` is a shell syntax error, which would take down the run
    the preflight exists to protect."""
    run = _run(
        tmp_path,
        containers=[("angry-db", "postgres:18")],
        sizes={"angry-db": "FATAL:  role does not exist"},
        floor=1 * GIB,
    )
    assert run.rc == 0, run.out
    assert run.required_bytes == 1 * GIB
    assert "angry-db did not answer the database-size query" in run.out


def test_the_sizing_probe_uses_the_per_container_superuser(tmp_path: Path):
    """Same resolution as the dump loop -- one implementation, so the probe
    and the dump can never disagree about which role to connect as."""
    run = _run(
        tmp_path,
        containers=[("bespoke-db", "postgres:18"), ("plain-db", "postgres:18")],
        sizes={"bespoke-db": str(GIB), "plain-db": str(GIB)},
        users={"bespoke-db": "erpnext"},
        floor=0,
    )
    assert run.rc == 0, run.out
    assert any("exec bespoke-db psql -U erpnext" in c for c in run.docker_calls)
    assert any("exec plain-db psql -U postgres" in c for c in run.docker_calls)


def test_an_insufficient_disk_aborts_before_any_dump_is_written(tmp_path: Path):
    """The whole point of a preflight: no pg_dumpall byte hits the staging
    mount when the space is not there."""
    run = _run(
        tmp_path,
        containers=[("db-1", "postgres:18")],
        sizes={"db-1": str(9 * GIB)},
        floor=1 * GIB,
        preflight_rc=1,
    )
    assert run.rc == 1, run.out
    assert run.required_bytes == 9 * GIB
    assert not any("pg_dumpall" in c for c in run.docker_calls)


def test_a_derived_requirement_is_enforced_with_no_static_floor(tmp_path: Path):
    """The invocation gate used to test BACKUP_PREFLIGHT_MIN_BYTES, so a host
    with no floor configured skipped the check entirely -- including the
    number just derived from its databases. It gates on the resolved
    requirement now."""
    run = _run(
        tmp_path,
        containers=[("db-1", "postgres:18")],
        sizes={"db-1": str(2 * GIB)},
        floor=None,
    )
    assert run.rc == 0, run.out
    assert run.required_bytes == 2 * GIB


def test_nothing_to_require_skips_the_check(tmp_path: Path):
    """No floor and no databases means there is no number to enforce. Calling
    the preflight with 0 would be a guaranteed pass dressed up as a check."""
    run = _run(tmp_path, containers=[], floor=None)
    assert run.rc == 0, run.out
    assert run.preflight_argv is None
    assert "no running postgres-ish containers found" in run.out
