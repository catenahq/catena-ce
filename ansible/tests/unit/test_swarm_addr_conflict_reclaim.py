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
    / "ansible" / "roles" / "infrastructure" / "tasks"
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


def test_classifiers_read_both_streams():
    """`docker stack deploy --detach=false` reports per-task errors on its
    stdout progress stream and CLI-level failures on stderr. Classifying on
    one half alone silently drops whole failure classes into "non-transient",
    which fails the converge on something a retry would have cleared."""
    for fragment, fact in (
        ("detect transient in deploy output", "_swarm_deploy_transient"),
        ("detect overlay address conflict", "_swarm_deploy_addr_conflict"),
    ):
        expr = _find(ATTEMPT, fragment)["ansible.builtin.set_fact"][fact]
        assert "stdout" in expr and "stderr" in expr


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


def test_deploy_is_synchronous_and_prunes():
    """--detach=false is what makes a non-zero rc mean "the stack did not come
    up". Detached, the command returns as soon as swarm ACCEPTS the spec, and
    every failure mode downstream of that reads as success. --prune is what
    makes removing a service from the compose actually remove it from the
    host."""
    argv = _find(ATTEMPT, "deploy attempt")["ansible.builtin.command"]["argv"]
    assert "--detach=false" in argv
    assert "--prune" in argv
