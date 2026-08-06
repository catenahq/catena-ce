"""The apt-update rescue must not fail on its own proof that apt works.

roles/common's "Refresh apt cache (with proxy-bypass fallback)" block exists
because the apt module folds its retry loop into an empty summary string, so a
flake reads as an unexplained failure. The rescue re-runs `apt-get update`
raw to recover the real error.

When that raw retry returns rc=0 there IS no real error: the index refreshed
and the module failure was the retry-loop artifact the block was written to
absorb. The rescue nonetheless aborted the play, printing

    apt update failed and no APT_PROXY_URL is configured, so
    there is nothing to bypass. Real apt-get error:
      rc=0
      stderr=

which reports success as the reason for failing. Observed on bench run
2026-08-06T21-55-42-fcd1 at stage-1 bootstrap. It is client-facing:
APT_PROXY_URL is empty on any install without an apt cacher -- and empty
during EVERY bootstrap regardless, since common_apt_proxy_url resolves from
the on-box config store which does not exist yet at that point.

Run: uv run pytest tests/unit/test_apt_rescue_respects_diagnostic.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TASKS = ANSIBLE / "roles" / "common" / "tasks" / "main.yml"

DIAG_RC_OK = "_apt_update_diag.rc != 0"


def _rescue_tasks() -> list[dict]:
    for task in yaml.safe_load(TASKS.read_text()):
        if "rescue" in task and "apt" in str(task.get("name", "")).lower():
            return task["rescue"]
    raise AssertionError("no apt block/rescue found -- has this been restructured?")


def _conditions(task: dict) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else [str(w) for w in when]


def _named(fragment: str) -> dict:
    for task in _rescue_tasks():
        if fragment.lower() in str(task.get("name", "")).lower():
            return task
    raise AssertionError(f"no rescue task matching {fragment!r}")


def test_the_diagnostic_itself_is_unconditional():
    """It has to run before anything can branch on its result, and it must
    never abort the play itself -- it is the thing recovering the message."""
    diag = _named("capture diagnostic")
    assert diag.get("failed_when") is False
    assert not diag.get("when"), "the diagnostic must not be conditional"


def test_failure_branch_requires_a_real_nonzero_rc():
    """The bug: this fired on rc=0, aborting the bootstrap while printing
    'Real apt-get error: rc=0'."""
    fail = _named("no proxy to bypass")
    conds = _conditions(fail)
    assert any(DIAG_RC_OK in c for c in conds), (
        "the fail branch does not check the diagnostic's rc, so a successful "
        f"retry still aborts the play. Conditions: {conds}"
    )
    assert any("common_apt_proxy_url" in c for c in conds), (
        "the fail branch no longer checks for a proxy to bypass"
    )


def test_every_workaround_step_is_gated_on_a_real_failure():
    """rc=0 means the index is refreshed. Dropping the proxy, re-running apt
    and rewriting the conf afterwards is work done to fix nothing."""
    for fragment in ("drop proxy conf", "direct, no proxy", "restore proxy conf"):
        conds = _conditions(_named(fragment))
        assert any(DIAG_RC_OK in c for c in conds), (
            f"rescue task {fragment!r} runs even when the diagnostic "
            f"succeeded. Conditions: {conds}"
        )


def test_proxy_conf_is_not_rewritten_when_no_proxy_is_configured():
    """With common_apt_proxy_url empty this wrote `Acquire::http::Proxy "";`
    -- re-creating the file the main path's "Remove apt HTTP proxy" task
    deletes, so a converge that entered the rescue stopped being idempotent."""
    conds = _conditions(_named("restore proxy conf"))
    assert any(
        "common_apt_proxy_url" in c and "length > 0" in c for c in conds
    ), f"the restore writes a proxy conf with no proxy configured: {conds}"
