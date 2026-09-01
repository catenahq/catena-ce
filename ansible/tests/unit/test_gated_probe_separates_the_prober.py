"""A probe that could not leave the machine is not a service verdict.

verify_gated_services probes the public URLs from the CONTROLLER. Its
failures come in two kinds and only one is about the gate:

  refused / reset / timeout / wrong status  -> the packet reached the edge
  ENETUNREACH / EHOSTUNREACH / no resolve   -> this machine has no route

The second failed a whole converge on bench run 2026-09-01T13-21-30-2bef:

    Status code was -1 and not [200, 301, 302, 307]: Request failed:
    <urlopen error [Errno 101] Network is unreachable>

The controller had cached an AAAA-only answer for the heartbeat hostname and
has no IPv6 egress, while the same wildcard served both families to an
uncached name and a cache flush made it resolve identically to its siblings.
Retries could not help: a cached answer does not change inside the window.
The repoint that converge belonged to was then recorded as a dead host, and
every scenario on that slot failed after it.

cli.py already draws this line for the bench's own tunnel probe. This holds
the same line in the role.

Run: uv run pytest tests/unit/test_gated_probe_separates_the_prober.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_TASKS = (Path(__file__).resolve().parents[2]
          / "roles" / "infrastructure" / "tasks" / "verify_gated_services.yml")


def _tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text(encoding="utf-8"))


def _by_name(name: str) -> dict:
    for t in _tasks():
        if t.get("name") == name:
            return t
    raise AssertionError(f"task {name!r} is gone from {_TASKS}")


def test_controller_side_faults_are_split_out():
    t = _by_name("Verify gated services: split controller-side uplink faults "
                 "from real ones")
    expr = str(t["ansible.builtin.set_fact"]["_infra_gated_unreachable"])
    for signature in (
        "Network is unreachable",
        "No route to host",
        "Name or service not known",
        "Temporary failure in name resolution",
    ):
        assert signature in expr, f"{signature!r} is not classified"


def test_the_failure_set_excludes_them():
    """The split is only worth anything if the fail task stops seeing them."""
    t = _by_name("Verify gated services: compute failure set")
    expr = str(t["ansible.builtin.set_fact"]["_infra_gated_failures"])
    assert "reject('in', _infra_gated_unreachable)" in expr


def test_a_real_failure_still_fails():
    """Refused, reset, timed out and wrong-status carry no such signature, so
    they stay in the failure set and the aggregated report still fires."""
    t = _by_name("Verify gated services: fail with aggregated report")
    assert str(t["when"]) == "_infra_gated_failures | length > 0"
    assert "ansible.builtin.fail" in t


def test_the_operator_is_told_rather_than_left_guessing():
    t = _by_name("Verify gated services: the CONTROLLER could not reach these")
    msg = str(t["ansible.builtin.debug"]["msg"])
    assert "not the" in msg and "gate" in msg
    assert "curl -4" in msg, "the message should say how to check by hand"
    assert str(t["when"]) == "_infra_gated_unreachable | length > 0"


def test_the_split_runs_before_the_failure_set_is_computed():
    names = [t.get("name", "") for t in _tasks()]
    split = names.index("Verify gated services: split controller-side uplink "
                        "faults from real ones")
    compute = names.index("Verify gated services: compute failure set")
    assert split < compute
