"""verify_gated_services fails on a probe that reached the edge and reports,
without failing, one that could not leave the controller
(reconcile/roles/infrastructure/tasks/verify_gated_services.yml says why).

Run: uv run pytest tests/unit/test_gated_probe_separates_the_prober.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_TASKS = (Path(__file__).resolve().parents[2]
          / "reconcile" / "roles" / "infrastructure" / "tasks" / "verify_gated_services.yml")


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
    assert "public_https.py" in msg, "the message should say how to check by hand"
    assert str(t["when"]) == "_infra_gated_unreachable | length > 0"


def test_the_split_runs_before_the_failure_set_is_computed():
    names = [t.get("name", "") for t in _tasks()]
    split = names.index("Verify gated services: split controller-side uplink "
                        "faults from real ones")
    compute = names.index("Verify gated services: compute failure set")
    assert split < compute
