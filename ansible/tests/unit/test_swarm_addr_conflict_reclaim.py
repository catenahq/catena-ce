"""Lock the swarm-stack overlay-address-conflict reclaim.

fi_o2 (operator Ctrl-C mid-converge) leaves a dangling overlay endpoint on
catena-network; the next deploy collides with "Address already in use". That
string is a retryable transient, but a BARE retry can never clear it -- the
leaked IP is still held. The keycloak deploy exhausted all attempts, each
failing identically.

These tests pin the fix in _swarm_stack_deploy_attempt.yml: on an address
conflict, reclaim leaked endpoints (container entirely gone) before the loop
retries. Timing-safe -- fires ONLY post-collision (never the every-converge
blanket sweep that raced swarm churn) and never touches an endpoint with a
live container, so no running service loses routing.

The leak survived the move off the Portainer stack API: it is a property of
overlay networking and an ungraceful interrupt, not of whatever asked for the
containers, so the reclaim moved with the retry loop rather than being retired
with it.

Run: uv run pytest tests/unit/test_swarm_addr_conflict_reclaim.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "reconcile" / "roles" / "infrastructure" / "tasks"
)
ATTEMPT = ROLE / "_swarm_stack_deploy_attempt.yml"
STACK = ROLE / "swarm_stack.yml"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _walk(tasks: list[dict]):
    """Yield every task, descending into block/rescue/always."""
    for t in tasks:
        yield t
        for key in ("block", "rescue", "always"):
            if isinstance(t.get(key), list):
                yield from _walk(t[key])


def _find(path: Path, name_fragment: str) -> dict:
    for t in _walk(_tasks(path)):
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_address_conflict_still_classified_transient():
    # The loop must keep retrying after a reclaim, so the string stays in
    # the transient set -- the reclaim is what makes the retry able to win.
    task = _find(ATTEMPT, "detect transient in deploy output")
    expr = task["ansible.builtin.set_fact"]["_swarm_deploy_transient"]
    assert "Address already in use" in expr


def test_addr_conflict_detected_separately():
    task = _find(ATTEMPT, "detect overlay address conflict")
    expr = task["ansible.builtin.set_fact"]["_swarm_deploy_addr_conflict"]
    assert "Address already in use" in expr


def test_classifiers_read_both_streams_and_the_per_task_errors():
    """`docker stack deploy --detach=false` reports per-task errors on its
    stdout progress stream and CLI-level failures on stderr. Classifying on
    one half alone silently drops whole failure classes into "non-transient",
    which fails the converge on something a retry would have cleared.

    Neither stream is enough. When swarm PAUSES a rolling update the stderr is
    only "<svcid>: service update paused: update paused due to failure or early
    termination of task <taskid>" -- an ID, not a reason. The reason is in that
    task's Error field, so the classifiers must read `docker service ps` too.
    Without it, "failed to set up container networking: Address already in
    use" -- listed as transient right here, with a reclaim step written for it
    -- failed the play (fi_a2_oidc_secret_rotation, bench
    2026-08-04T05-25-31-68a4) while the next task on that service came up
    Running.
    """
    for fragment, fact in (
        ("detect transient in deploy output", "_swarm_deploy_transient"),
        ("detect overlay address conflict", "_swarm_deploy_addr_conflict"),
    ):
        expr = _find(ATTEMPT, fragment)["ansible.builtin.set_fact"][fact]
        assert "stdout" in expr and "stderr" in expr
        assert "_swarm_diag_taskps" in expr, (
            f"{fragment} cannot see a paused update's cause without the "
            f"per-task errors"
        )


def test_the_per_task_diag_runs_before_classification_and_is_ungated():
    """A pre-fail diagnostic, gated on `not transient` and placed after the
    classifiers, collects the one output carrying a paused update's cause only
    after the code has already decided the cause is unknown."""
    names = [t.get("name") or "" for t in _walk(_tasks(ATTEMPT))]
    diag = next(i for i, n in enumerate(names) if "diag -- per-task state" in n)
    classify = next(
        i for i, n in enumerate(names) if "detect transient in deploy output" in n
    )
    assert diag < classify, (
        "the classifier reads _swarm_diag_taskps; collecting it afterwards "
        "leaves the fact undefined at the moment it is needed"
    )
    assert "when" not in _find(ATTEMPT, "diag -- per-task state"), (
        "gating the collection on the classification it feeds is circular"
    )


def test_reclaim_gated_on_conflict_only():
    task = _find(ATTEMPT, "reclaim leaked")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else when
    assert "_swarm_deploy_addr_conflict" in cond


def test_reclaim_skips_swarm_lb_self_and_gateway():
    task = _find(ATTEMPT, "reclaim leaked")
    script = task["ansible.builtin.shell"]
    # swarm LB, network self-entry, and the gateway *-endpoint sandbox
    # have no backing container by design and must never be disconnected.
    assert 'case "$name" in "$net"|lb-*|*-endpoint) continue' in script


def test_reclaim_only_targets_gone_containers():
    task = _find(ATTEMPT, "reclaim leaked")
    script = task["ansible.builtin.shell"]
    # a live OR merely-stopped container inspects fine -> skipped; only an
    # endpoint whose container is entirely gone is a leak.
    assert "docker inspect" in script
    assert "continue" in script
    assert "docker network disconnect -f" in script


def test_addr_conflict_fact_initialized():
    task = _find(STACK, "initialize deploy attempt state")
    facts = task["ansible.builtin.set_fact"]
    assert facts.get("_swarm_deploy_addr_conflict") is False


def test_the_deploy_itself_never_reports_changed():
    """`docker stack deploy` is a reconcile: it sends the same spec every
    converge and swarm restarts nothing when the spec is unchanged, so a
    successful run is the steady state. `changed_when: rc == 0` made every
    re-converge report changed and broke fi_a4_master_realm_idempotent,
    which asserts `--tags keycloak` on a converged host changes nothing.

    The change signal belongs to the stack-file write, which is the
    declaration."""
    deploy = _find(ATTEMPT, "deploy attempt")
    assert deploy["changed_when"] is False

    write = _find(STACK, "write the stack file")
    assert "changed_when" not in write, (
        "the file write is the declaration; suppressing its change signal "
        "would leave nothing in the path reporting a real spec change"
    )


def test_deploy_is_synchronous_and_prunes():
    """--detach=false is what makes a non-zero rc mean "the stack did not come
    up". Detached, the command returns as soon as swarm ACCEPTS the spec, and
    every failure mode downstream of that reads as success. --prune is what
    makes removing a service from the compose actually remove it from the
    host."""
    argv = _find(ATTEMPT, "deploy attempt")["ansible.builtin.command"]["argv"]
    assert "--detach=false" in argv
    assert "--prune" in argv
