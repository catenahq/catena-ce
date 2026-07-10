"""Unit tests for scripts/run-disk-watch.sh threshold + emit logic.

Runs the real script under /bin/sh with df / curl / hostname stubbed on
PATH (curl logs its argv to a file instead of hitting the network), so
the warn/crit/ok classification and the ntfy + Healthchecks emissions
are asserted without a host.

Run: uv run pytest tests/unit/test_disk_watch.py
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "scripts" / "run-disk-watch.sh"
)


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _run(tmp_path: Path, *, pct: int, env_extra: str = "") -> tuple[str, list[str]]:
    """Execute the script with a stub df reporting `pct`% on every path.
    Returns (stdout+stderr, list of curl argv lines)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl_log = tmp_path / "curl.log"
    _write(bin_dir / "df", (
        "#!/bin/sh\n"
        'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
        f'echo "/dev/sda1 100 {pct} {100 - pct} {pct}% /"\n'
    ))
    _write(bin_dir / "curl", (
        "#!/bin/sh\n"
        f'echo "$@" >> {curl_log}\n'
    ))
    _write(bin_dir / "hostname", "#!/bin/sh\necho testhost\n")

    env_file = tmp_path / "disk-watch.env"
    env_file.write_text(
        "DISK_WATCH_PATHS=/\n"
        "DISK_WATCH_WARN_PCT=80\n"
        "DISK_WATCH_CRIT_PCT=90\n"
        "DISK_WATCH_SLUG=disk-usage\n"
        "HEALTHCHECKS_LOOPBACK_PORT=18000\n"
        "HC_PING_KEY=pingkey\n"
        "NTFY_SERVER=https://ntfy.example\n"
        "NTFY_TOPIC=alerts\n"
        + env_extra
    )

    proc = subprocess.run(
        ["/bin/sh", str(SCRIPT), str(env_file)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stderr
    calls = curl_log.read_text().splitlines() if curl_log.exists() else []
    return proc.stdout + proc.stderr, calls


def test_ok_pings_success_only(tmp_path):
    out, calls = _run(tmp_path, pct=42)
    assert "disk usage OK" in out
    assert len(calls) == 1
    assert "/ping/pingkey/disk-usage?create=1" in calls[0]
    assert "/fail" not in calls[0]
    assert "ntfy.example" not in calls[0]


def test_warn_publishes_ntfy_high_and_stays_green(tmp_path):
    out, calls = _run(tmp_path, pct=85)
    assert "disk usage WARNING" in out
    ntfy = [c for c in calls if "ntfy.example/alerts" in c]
    hc = [c for c in calls if "/ping/pingkey/" in c]
    assert len(ntfy) == 1 and "high" in ntfy[0]
    assert len(hc) == 1 and "/fail" not in hc[0]


def test_crit_publishes_urgent_and_fails_the_check(tmp_path):
    out, calls = _run(tmp_path, pct=95)
    assert "disk usage CRITICAL" in out
    ntfy = [c for c in calls if "ntfy.example/alerts" in c]
    hc = [c for c in calls if "/ping/pingkey/" in c]
    assert len(ntfy) == 1 and "urgent" in ntfy[0]
    assert len(hc) == 1 and "disk-usage/fail?create=1" in hc[0]


def test_thresholds_are_inclusive(tmp_path):
    out, _ = _run(tmp_path, pct=90)
    assert "disk usage CRITICAL" in out
    out, _ = _run(tmp_path, pct=80)
    assert "disk usage WARNING" in out
    out, _ = _run(tmp_path, pct=79)
    assert "disk usage OK" in out


def test_force_pct_override_drives_the_emit_path(tmp_path):
    # The bench uses DISK_WATCH_FORCE_PCT to prove the alert path
    # without filling a disk.
    out, calls = _run(tmp_path, pct=10, env_extra="DISK_WATCH_FORCE_PCT=95\n")
    assert "disk usage CRITICAL" in out
    assert any("disk-usage/fail?create=1" in c for c in calls)


def test_unwired_degrades_to_journal_only(tmp_path):
    bin_dir = tmp_path / "bin"
    out, calls = _run(
        tmp_path, pct=95,
        env_extra="HC_PING_KEY=\nNTFY_TOPIC=\n",
    )
    assert "disk usage CRITICAL" in out
    assert calls == []
