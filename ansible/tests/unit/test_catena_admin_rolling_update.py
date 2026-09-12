"""A paused rolling update is a state to read, not a verdict.

`docker service update` waits for the rolling update and exits 1 the moment
swarm PAUSES it, which swarm does on the first task that dies. The panel
publishes its native-login port with mode=host, so on a single node the
incoming task can lose a race with the outgoing task's port:

    fatal task error: starting container failed: failed to set up container
    networking: Address already in use

Swarm's reconciler starts a replacement seconds later and the service is up on
the new spec. A converge that reads the exit code as the outcome therefore
fails on a host whose panel is healthy, so the exit code does not decide alone:
the wait and the drift re-inspect do.

These are structural assertions against the task file, like
test_schedule_single_owner.py. They fail when the rc becomes the judge again.

Run: uv run pytest tests/unit/test_catena_admin_rolling_update.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ADMIN_DEPLOY = ANSIBLE / "reconcile" / "roles" / "catena-admin" / "tasks" / "deploy.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(ADMIN_DEPLOY.read_text(encoding="utf-8"))


def _named(fragment: str) -> list[dict]:
    return [t for t in _tasks() if fragment in str(t.get("name", ""))]


def test_the_rolling_update_does_not_fail_the_converge_on_rc_alone():
    update = _named("reconcile the service spec")
    assert len(update) == 1, "exactly one task applies the drift"
    assert update[0].get("failed_when") is False, (
        "a non-zero rc reports that swarm paused the update, not that the "
        "update did not land -- the host has to be asked which it was"
    )


def test_the_reconcile_waits_for_a_task_to_come_back_up():
    wait = _named("wait for the reconciled service to converge")
    assert len(wait) == 1, (
        "without a wait, 'did it land' is answered by the exit code again"
    )
    argv = " ".join(str(a) for a in wait[0]["ansible.builtin.command"]["argv"])
    assert "service ps" in argv
    assert "desired-state=running" in argv, (
        "a stop-first update leaves the outgoing task listed while it drains, "
        "so an unfiltered ps answers with the task being replaced"
    )
    assert "^Running" in str(wait[0]["until"])
    assert wait[0].get("retries")


def test_a_task_that_never_starts_still_fails_the_converge():
    fail = _named("reconciled service never came back up")
    assert len(fail) == 1
    msg = str(fail[0]["ansible.builtin.fail"]["msg"])
    assert "_ca_reconverged.stdout" in msg, (
        "the Error column is the reason; a failure that does not quote it "
        "sends the operator back to the host to find out what swarm said"
    )


def test_the_drift_re_inspect_still_runs_after_the_reconcile():
    settled = _named("settled the drift it applied")
    assert len(settled) == 1, (
        "the re-inspect is what makes tolerating the rc safe: it is the check "
        "that the flags actually reached the spec"
    )
