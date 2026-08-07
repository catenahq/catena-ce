"""The coturn consumer gate must answer the same way everywhere, every time.

coturn deploys only when a real-time app that needs TURN is running, and
validate asserts coturn is present under the same condition. Those are two
separate probes, minutes apart in one converge, and they have to agree.

They did not. On bench f140 backup_rollback the deploy gate reported "No TURN
consumer running" and skipped, and the validate gate then failed the converge
demanding a coturn service -- because in between, the restored Talk HPB
container entered a crash loop (`You need to provide the TURN_SECRET.`), and
bare `docker ps` lists RESTARTING containers. A container that cannot start is
not a TURN consumer, and gating on one deploys a relay nothing can use.

Run: uv run pytest tests/unit/test_coturn_consumer_gate.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COTURN = ANSIBLE / "roles" / "coturn" / "tasks"

_DEPLOY_PROBE = "coturn: detect a running consumer (Talk HPB or a Jitsi JVB)"
_VALIDATE_PROBE = (
    "validate coturn: detect a running consumer (Talk HPB or a Jitsi JVB)"
)


def _probe(filename: str, task_name: str) -> str:
    tasks = yaml.safe_load((COTURN / filename).read_text(encoding="utf-8"))
    task = next(t for t in tasks if t.get("name") == task_name)
    return task["ansible.builtin.shell"]


def test_both_probes_ignore_restarting_containers():
    for filename, name in (
        ("main.yml", _DEPLOY_PROBE),
        ("validate.yml", _VALIDATE_PROBE),
    ):
        body = _probe(filename, name)
        for service in ("talk-hpb", "jvb"):
            line = next(
                ln for ln in body.splitlines() if f"service={service}" in ln
            )
            assert "--filter status=running" in line, (
                f"{filename} {service} probe counts restarting containers; a "
                "crash-looping consumer is not a consumer"
            )


def test_the_two_probes_are_identical():
    """Two spellings of "is a consumer running" drift into disagreeing about
    the same host, which is how one converge both skipped the deploy and
    failed the validate."""
    assert (
        _probe("main.yml", _DEPLOY_PROBE)
        == _probe("validate.yml", _VALIDATE_PROBE)
    )
