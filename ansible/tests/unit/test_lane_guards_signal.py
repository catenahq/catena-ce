"""A lane that cannot report must not exit 0.

The pattern across these scripts: `if [ -z "$SOMETHING" ]; then log; exit 0`.
Reasonable-looking, and wrong in one specific case -- when the missing thing
is the REPORTING channel itself. Then the exit is the last signal available,
and spending it on 0 means the lane runs nightly, does nothing, and leaves no
trace anywhere: no Healthchecks check exists to go late (the checks are
auto-provisioned by the first ping), no failed unit, nothing in the panel.

vault_healthchecks_ping_key is INTERNAL_SECRETS -- minted on the box by the
converge loader every run -- so an empty HC_PING_KEY is never a configuration.
It means the on-box store did not load when the env file was rendered.

Run: uv run pytest tests/unit/test_lane_guards_signal.py
"""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _code(name: str) -> list[str]:
    """Script lines with comments stripped: several of these headers describe
    the old behaviour deliberately, and that must not read as the code."""
    return [ln for ln in (SCRIPTS / name).read_text().splitlines()
            if not ln.lstrip().startswith("#")]


def _guard_exit(name: str, needle: str) -> str:
    """The `exit N` closing the first guard whose test mentions `needle`."""
    lines = _code(name)
    for i, ln in enumerate(lines):
        if needle in ln and ln.lstrip().startswith("if "):
            for tail in lines[i:i + 12]:
                if tail.strip().startswith("exit "):
                    return tail.strip()
            raise AssertionError(f"{name}: no exit inside the {needle} guard")
    raise AssertionError(f"{name}: no guard testing {needle}")


@pytest.mark.parametrize("script", [
    "run-clamav-watch.sh",
    "run-mail-canary.sh",
])
def test_an_unwired_ping_key_fails_the_unit(script):
    """These two scripts provision their Healthchecks check with the first
    ping (?create=1). With no key, no check is ever created -- so nothing can
    go late and the lane's silence is indistinguishable from a clean board.
    systemd is the only remaining observer."""
    assert _guard_exit(script, "HC_PING_KEY") == "exit 1"


@pytest.mark.parametrize("script", [
    "run-clamav-watch.sh",
    "run-mail-canary.sh",
])
def test_the_guard_names_the_file_to_fix(script):
    body = (SCRIPTS / script).read_text()
    assert "$ENV_FILE" in body
    assert "converge" in body, "the log must say how to fix it, not just what broke"


def test_the_due_gate_leaves_a_trace():
    """run-backup's not-due skip is NOT a problem -- a recent success is why
    it skips, and the succeeded check is inside its grace window, so /fail
    would page a healthy host. But it was the only guard in that file with no
    ping at all, which makes "the timer fired and declined" and "the timer
    never fired" the same record. /log records the event without touching
    up/down state."""
    lines = _code("run-backup.sh")
    idx = next(i for i, ln in enumerate(lines) if "backup not due yet" in ln)
    window = "\n".join(lines[idx:idx + 6])
    assert "ping_hc_attempted /log" in window
    assert "/fail" not in window, "a not-due skip must not page"


def test_coverage_cannot_report_covered_when_it_checked_nothing():
    """rc 0 means VERIFIED covered. An unrendered paths file used to return
    it, so the one check that finds data outside the backup set reported that
    everything was inside it."""
    assert _guard_exit("backup-coverage.sh", "COVERAGE_PATHS_FILE") == "exit 3"
