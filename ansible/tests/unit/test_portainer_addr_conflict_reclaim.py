"""Lock the Portainer-stack overlay-address-conflict reclaim.

fi_o2 (operator Ctrl-C mid-converge) leaves a dangling overlay endpoint
on catena-network; the next compose-up collides with HTTP 500 "Address
already in use". That string is a retryable transient, but a BARE retry
can never clear it -- the leaked IP is still held. The keycloak stack
deploy exhausted all attempts, each failing identically.

These tests pin the fix in _portainer_stack_deploy_attempt.yml: on an
address conflict, reclaim leaked endpoints (container entirely gone)
before the loop retries. Timing-safe -- fires ONLY post-collision (never
the every-converge blanket sweep that raced swarm churn) and never
touches an endpoint with a live container, so no running service loses
routing.

Run: uv run pytest tests/unit/test_portainer_addr_conflict_reclaim.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "infrastructure" / "tasks"
)
ATTEMPT = ROLE / "_portainer_stack_deploy_attempt.yml"
STACK = ROLE / "portainer_stack.yml"


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
    task = _find(ATTEMPT, "detect transient in deploy response")
    expr = task["ansible.builtin.set_fact"]["_portainer_deploy_transient"]
    assert "Address already in use" in expr


def test_addr_conflict_detected_separately():
    task = _find(ATTEMPT, "detect overlay address conflict")
    expr = task["ansible.builtin.set_fact"]["_portainer_deploy_addr_conflict"]
    assert "Address already in use" in expr


def test_reclaim_gated_on_conflict_only():
    task = _find(ATTEMPT, "reclaim leaked")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else when
    assert "_portainer_deploy_addr_conflict" in cond


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
    assert facts.get("_portainer_deploy_addr_conflict") is False
