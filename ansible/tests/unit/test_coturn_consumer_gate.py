"""The coturn consumer gate is asked ONCE, by the half that can act on it.

coturn deploys only when a real-time app that needs TURN is running. That
question belongs to main.yml, where roles/coturn sits in converge.yml's order. A
validate that asks it again, minutes later in the same converge, and asserts
coturn's presence on its own answer is a second probe that has to agree with the
first and cannot.

Two independent reasons it cannot:

  a consumer in a crash loop -- a restored Talk HPB without its secret
  (`You need to provide the TURN_SECRET.`) restarts forever, and bare
  `docker ps` lists RESTARTING containers, so one probe can skip the deploy
  while a later one demands coturn. The deploy probe filters on
  `--filter status=running`: a container that cannot start is no consumer.

  a consumer that arrives mid-converge -- a DR restore brings Nextcloud and its
  Talk HPB up LATER IN THE SAME CONVERGE, after roles/coturn has already run and
  correctly skipped. main.yml documents that lag as intended -- coturn "lands on
  the converge after the consumer is first deployed" -- so two probes are both
  right and still disagree.

The second one cannot be fixed by making the probes match, because the
disagreement is about WHEN, not about what counts as a consumer. So validate
does not ask: it asserts what the host HAS, which is what its own comment says
(`Assert-if-present, not assert-if-wanted`).

Run: uv run pytest tests/unit/test_coturn_consumer_gate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COTURN = ANSIBLE / "reconcile" / "roles" / "coturn" / "tasks"

_DEPLOY_PROBE = "coturn: detect a running consumer (Talk HPB or a Jitsi JVB)"


def _tasks(filename: str) -> list[dict]:
    return yaml.safe_load((COTURN / filename).read_text(encoding="utf-8"))


def _probe(filename: str, task_name: str) -> str:
    task = next(t for t in _tasks(filename) if t.get("name") == task_name)
    return task["ansible.builtin.shell"]


def test_the_deploy_probe_ignores_restarting_containers():
    """A container that cannot start is not a TURN consumer, and gating on one
    deploys a relay nothing can use."""
    body = _probe("main.yml", _DEPLOY_PROBE)
    for service in ("talk-hpb", "jvb"):
        line = next(
            ln for ln in body.splitlines() if f"vps.component={service}" in ln
        )
        assert "--filter status=running" in line, (
            f"main.yml {service} probe counts restarting containers; a "
            "crash-looping consumer is not a consumer"
        )


def test_validate_does_not_probe_for_a_consumer_at_all():
    """Validate leaves the consumer question to main.yml.

    Identical probe spellings cannot help: both probes are correct and still
    disagree when the consumer appears between them. The only fix is for
    validate not to ask.
    """
    src = (COTURN / "validate.yml").read_text(encoding="utf-8")
    assert "vps.component=talk-hpb" not in src and "vps.component=jvb" not in src, (
        "validate.yml probes for a TURN consumer again. Whether one is running "
        "is main.yml's question; asking it at the end of the same converge "
        "makes a DR restore that deploys its consumer mid-converge fail on a "
        "coturn the deploy pass deliberately deferred."
    )


def test_the_validate_gate_is_presence_only():
    """assert-if-present, which is what the file's own comment says."""
    gate = next(t for t in _tasks("validate.yml")
                if t.get("name") == "validate coturn: resolve the assertion gate")
    expr = str(gate["ansible.builtin.set_fact"]["_v_coturn_expected"])
    assert "_v_coturn_existing" in expr, (
        "the gate no longer reads whether a coturn service exists"
    )
    assert "_v_coturn_consumer" not in expr, (
        "the gate reads a running consumer again -- that is assert-if-wanted, "
        "and it fails a host whose coturn is one converge away by design"
    )
