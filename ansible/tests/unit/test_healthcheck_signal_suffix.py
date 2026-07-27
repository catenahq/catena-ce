"""The Healthchecks signal suffix must land in the PATH, not the query.

The auto-provisioned check URLs end in "?create=1" (roles/backup/defaults
auto-creates the check on first ping). run-backup.sh used to build its
pings as "${URL}${suffix}", which produced

    https://hc.example/ping/<key>/catena-backup-attempted?create=1/fail

Healthchecks reads the SIGNAL from the path, so that is a plain ping on
the bare check: a SUCCESS. Every hard failure -- a pg_dumpall abort, an
unreachable repo -- pinged the immediate-page lane as a healthy run. The
lane was wired, provisioned and reporting the opposite of the truth.

These tests exercise the real hc_url function out of the shipped script,
not a copy of it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE / "scripts" / "run-backup.sh"


def _extract_fn(path: Path, name: str) -> str:
    """The named POSIX-sh function, verbatim, from a script that cannot be
    sourced wholesale (run-backup.sh sources an env file and runs restic at
    import time)."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _hc_url(base: str, suffix: str) -> str:
    fn = _extract_fn(SCRIPT, "hc_url")
    script = f'{fn}\nhc_url "$1" "$2"\n'
    out = subprocess.run(
        ["sh", "-c", script, "sh", base, suffix],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


@pytest.mark.parametrize("suffix", ["/fail", "/start"])
def test_suffix_goes_before_the_query_string(suffix: str):
    base = "https://hc.example/ping/KEY/catena-backup-attempted?create=1"
    got = _hc_url(base, suffix)
    assert got == (
        f"https://hc.example/ping/KEY/catena-backup-attempted{suffix}?create=1"
    ), got
    # The exact shape of the old bug.
    assert not got.endswith(f"create=1{suffix}")


def test_plain_url_without_a_query_is_unchanged_apart_from_the_suffix():
    got = _hc_url("https://hc-ping.com/UUID", "/fail")
    assert got == "https://hc-ping.com/UUID/fail"


def test_empty_suffix_is_the_bare_ping():
    base = "https://hc.example/ping/KEY/catena-backup-succeeded?create=1"
    assert _hc_url(base, "") == base


def test_run_backup_pings_through_the_helper():
    """A future edit that goes back to plain concatenation must fail here,
    not silently on a client's host at 03:00."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"${BACKUP_HEALTHCHECK_URL}${suffix}"' not in text
    assert '"${BACKUP_HEALTHCHECK_ATTEMPTED_URL}${suffix}"' not in text
    assert 'hc_url "${BACKUP_HEALTHCHECK_URL}"' in text
    assert 'hc_url "${BACKUP_HEALTHCHECK_ATTEMPTED_URL}"' in text


def test_unconfigured_and_disabled_backups_ping_the_alert_lane():
    """A host with no backup configuration, or with backups explicitly
    disabled, used to exit 0 before any ping. The check auto-provisions on
    first ping, so such a host had no check at all: nothing to go late,
    nothing to alert, and no way to notice a client running unbacked."""
    text = SCRIPT.read_text(encoding="utf-8")
    unconfigured = text.index("backup not configured")
    disabled = text.index("backup disabled in catena-admin")
    for start in (unconfigured, disabled):
        window = text[start:start + 200]
        assert "ping_hc_attempted /fail" in window, window
