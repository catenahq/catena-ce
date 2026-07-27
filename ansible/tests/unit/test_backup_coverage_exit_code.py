"""An application writing outside the backup set must fail the run.

backup-coverage.sh used to exit 0 unconditionally, by contract. A client
compose with an absolute bind mount outside backup_paths therefore produced
one line in a green run's journal, and nobody reads a green run's journal.
The path stayed out of every snapshot until somebody needed it back.

The check runs AFTER the restic backup, which is what makes failing on it
safe: the snapshot is already taken, so the covered data is captured and
the operator is paged about what is not. Failing before the backup would
trade "some data unbacked" for "no data backed up".

These tests drive the real shipped script against a fake `docker`.

Run: uv run pytest tests/unit/test_backup_coverage_exit_code.py
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]
COVERAGE = ANSIBLE / "scripts" / "backup-coverage.sh"
RUN_BACKUP = ANSIBLE / "scripts" / "run-backup.sh"


def _fake_docker(tmp_path: Path, mounts: list[tuple[str, str]]) -> Path:
    """A `docker` that reports one container with the given (type, source)
    mounts, matching the two invocations the script makes."""
    rows = "\n".join(f"{t}|{src}|app-1" for t, src in mounts)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  "ps --format {{.Names}}") echo app-1 ;;\n'
        f"  inspect*) printf '%s\\n' '{rows}' ;;\n"
        "esac\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(tmp_path: Path, mounts: list[tuple[str, str]]) -> subprocess.CompletedProcess:
    paths = tmp_path / "backup-coverage.paths"
    paths.write_text("/mnt/data/docker/volumes\n/etc/catena\n")
    env = dict(os.environ)
    env["COVERAGE_PATHS_FILE"] = str(paths)
    env["PATH"] = f"{_fake_docker(tmp_path, mounts)}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(COVERAGE)], capture_output=True, text=True, env=env,
    )


def test_a_covered_host_exits_zero(tmp_path: Path):
    res = _run(tmp_path, [("bind", "/mnt/data/docker/volumes/app")])
    assert res.returncode == 0, res.stdout + res.stderr


def test_an_uncovered_bind_mount_exits_two(tmp_path: Path):
    res = _run(tmp_path, [("bind", "/srv/app-data")])
    assert res.returncode == 2, res.stdout + res.stderr
    assert "/srv/app-data" in res.stdout


def test_named_volumes_and_system_paths_do_not_trip_it(tmp_path: Path):
    res = _run(tmp_path, [
        ("volume", "/var/lib/docker/volumes/app/_data"),
        ("bind", "/proc"),
        ("bind", "/var/run/docker.sock"),
        ("bind", "/mnt/data"),
    ])
    assert res.returncode == 0, res.stdout + res.stderr


def test_an_unrendered_paths_file_is_not_a_finding(tmp_path: Path):
    # A host converging for the first time has no paths file yet. Absence of
    # an answer is not evidence of a gap, and failing here would fail every
    # first backup.
    env = dict(os.environ)
    env["COVERAGE_PATHS_FILE"] = str(tmp_path / "absent")
    res = subprocess.run(
        ["bash", str(COVERAGE)], capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_the_wrapper_separates_a_finding_from_a_broken_check():
    # rc 2 is a finding and fails the run; anything else (including 124 from
    # the timeout) means the check could not run, which is not a finding.
    body = RUN_BACKUP.read_text(encoding="utf-8")
    assert '"${_cov_rc:-0}" = 2' in body, "rc 2 must be handled as a finding"
    assert "exit 4" in body, "a coverage finding must fail the run"
    assert '"${_cov_rc:-0}" != 0' in body, (
        "a check that errored or timed out must stay non-fatal"
    )
    # The rc has to be captured before the pipe: this script does not set
    # pipefail, so `cmd | sed` reports sed's rc, which is always 0.
    assert "_cov_out=$(timeout 120" in body, (
        "piping the checker straight into sed would discard its exit code"
    )


def test_the_coverage_check_runs_after_the_backup():
    # The snapshot must already exist when this fails, or the fix would cost
    # more data than the bug.
    body = RUN_BACKUP.read_text(encoding="utf-8")
    assert body.index("restic backup") < body.index("BACKUP_COVERAGE_SCRIPT")
