"""Lock the gated-services probe retry.

The end-of-role public-URL probe was single-shot: a transient failure to
reach the public URL from the probe vantage (a Cloudflare edge flap, a
momentary route/DNS blip, the SSO stack taking a few more seconds) failed
the whole converge even though the service was fine. A real client's
install must not die on a 10s network blip. The probe now retries; a
genuinely misconfigured gate never returns 200/30x and still fails after
the window (the aggregate report is unchanged).

Run: uv run pytest tests/unit/test_gated_services_probe_retry.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "infrastructure" / "tasks"
    / "verify_gated_services.yml"
)


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for t in _tasks():
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_probe_has_bounded_retries():
    task = _find("probe public URLs")
    assert "retries" in task
    assert "delay" in task
    assert "until" in task


def test_until_accepts_only_good_status():
    task = _find("probe public URLs")
    cond = task["until"]
    # the same success set as status_code; a connect failure (-1) keeps retrying
    for code in ("200", "301", "302", "307"):
        assert code in cond
    assert "status" in cond


def test_probe_still_ignores_errors_for_aggregate_report():
    # after retries exhaust, ignore_errors lets the compute-failure-set +
    # aggregate fail task own the hard failure (unchanged behaviour).
    task = _find("probe public URLs")
    assert task["ignore_errors"] is True


def test_aggregate_fail_task_still_present():
    # the hard gate on a genuinely-broken service must survive the retry change.
    task = _find("fail with aggregated report")
    assert "ansible.builtin.fail" in task
