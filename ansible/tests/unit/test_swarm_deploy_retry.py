"""Which failed swarm stack deploy attempts are retried, and what a failure says.

A deploy attempt is bounded by `timeout` (test_swarm_deploy_is_bounded.py), so
a task that can never converge ends as rc=124. Only an image pull still in
progress or failing earns that timeout another attempt: a crash loop or a task
no node accepts waits the same way on every attempt. The failure that ends the
retries carries the per-task state, which is where swarm puts the cause.

Run: uv run pytest tests/unit/test_swarm_deploy_retry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from ansible_tree import walk_tasks

ANSIBLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANSIBLE / "playbooks" / "filter_plugins"))

from swarm_deploy_retry import FilterModule, swarm_deploy_transient  # noqa: E402

TASKS = ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks"
ATTEMPT = TASKS / "_swarm_stack_deploy_attempt.yml"
STACK = TASKS / "swarm_stack.yml"

TIMED_OUT = 124

# `docker service ps --no-trunc` rows as the attempt file's diagnostic prints
# them, one block per service of the stack.
CRASH_LOOP = """==== keycloak_server ====
ID                          CURRENT STATE            ERROR
q1r2s3t4u5v6w7x8y9z0a1b2c   Ready 2 seconds ago
v0ws2k7m3n4p5q6r7s8t9u0v1   Failed 8 seconds ago     "task: non-zero exit (1)"
"""
UNPLACEABLE = """==== keycloak_server ====
ID                          CURRENT STATE            ERROR
h3j4k5l6m7n8p9q0r1s2t3u4v   Pending 10 minutes ago   "no suitable node (insufficient resources on 1 node)"
"""
PULLING = """==== keycloak_server ====
ID                          CURRENT STATE            ERROR
g4oilm5n6p7q8r9s0t1u2v3w4   Preparing 9 minutes ago
"""
PULL_FAILED = """==== keycloak_server ====
ID                          CURRENT STATE            ERROR
a1b2c3d4e5f6g7h8i9j0k1l2m   Ready 1 second ago
n3o4p5q6r7s8t9u0v1w2x3y4z   Rejected 6 seconds ago   "pull access denied for catena/keycloak, repository does not exist or may require 'docker login'"
"""
PULL_5XX = """==== keycloak_server ====
ID                          CURRENT STATE            ERROR
b2c3d4e5f6g7h8i9j0k1l2m3n   Rejected 4 seconds ago   "failed to do request: Head \\"https://quay.io/v2/phasetwo/phasetwo-keycloak/manifests/26\\": 503 Service Unavailable"
"""


def _task(path: Path, fragment: str) -> dict:
    return next(t for t in walk_tasks(yaml.safe_load(path.read_text()))
                if fragment in str(t.get("name", "")))


@pytest.mark.parametrize("tasks", [CRASH_LOOP, UNPLACEABLE], ids=["crash-loop", "no-suitable-node"])
def test_a_timeout_with_no_pull_is_not_transient(tasks: str) -> None:
    assert not swarm_deploy_transient(TIMED_OUT, "", tasks)


@pytest.mark.parametrize("tasks", [PULLING, PULL_FAILED, PULL_5XX], ids=["preparing", "pull-error", "registry-5xx"])
def test_a_timeout_during_a_pull_is_transient(tasks: str) -> None:
    assert swarm_deploy_transient(TIMED_OUT, "", tasks)


def test_a_timeout_reads_the_task_state_not_the_killed_progress_stream() -> None:
    """The progress stream a killed deploy leaves holds whatever it printed
    last; a crash loop's task state decides, whatever that stream says."""
    assert not swarm_deploy_transient(TIMED_OUT, "overall progress: 0 out of 1 tasks\n"
                                      "1/1: preparing\nverify: i/o timeout", CRASH_LOOP)


def test_a_stack_that_converged_despite_the_paused_update_is_retried() -> None:
    assert swarm_deploy_transient(1, "service update paused", CRASH_LOOP, converged=True)


@pytest.mark.parametrize("seen", [
    "failed to update service keycloak_server: Error response from daemon: "
    "rpc error: code = Unknown desc = update out of sequence",
    "failed to update service gatus_app: Error response from daemon: rpc error: "
    "code = DeadlineExceeded desc = stream terminated by RST_STREAM with error code: CANCEL",
], ids=["late-commit", "grpc-deadline"])
def test_a_manager_deadline_is_transient(seen: str) -> None:
    """A DeadlineExceeded update still commits once the swarm manager catches
    up, so the retry after it carries a stale version and is refused; the next
    attempt re-reads the version."""
    assert swarm_deploy_transient(1, seen, "")


def test_an_error_only_the_task_state_carries_is_read() -> None:
    """A paused update's stderr names a task id; the cause is in its Error."""
    stderr = ("abc123: service update paused: update paused due to failure or "
              "early termination of task def456")
    tasks = ('def456   Failed 1 second ago   "failed to set up container '
             'networking: Address already in use"')
    assert swarm_deploy_transient(1, stderr, tasks)


def test_a_crash_loop_that_exits_is_not_transient() -> None:
    assert not swarm_deploy_transient(1, "service update paused", CRASH_LOOP)


def test_the_classifier_hands_the_filter_the_per_task_state() -> None:
    expr = _task(ATTEMPT, "detect transient in deploy output")[
        "ansible.builtin.set_fact"]["_swarm_deploy_transient"]
    assert "swarm_deploy_transient(" in expr
    assert "_swarm_diag_taskps.stdout" in expr
    assert "_swarm_converged_probe" in expr


@pytest.mark.parametrize("path,fragment", [
    (ATTEMPT, "report transient, will retry"),
    (ATTEMPT, "fail hard on non-transient deploy error"),
    (STACK, "fail if all retries exhausted"),
], ids=["retry", "give-up", "exhausted"])
def test_each_retry_and_failure_prints_the_per_task_state(path: Path, fragment: str) -> None:
    msg = _task(path, fragment)["ansible.builtin.debug" if "report" in fragment
                                else "ansible.builtin.fail"]["msg"]
    assert "_swarm_diag_taskps.stdout" in msg, msg


def test_filter_module_exposes_the_filter() -> None:
    assert set(FilterModule().filters()) == {"swarm_deploy_transient"}
