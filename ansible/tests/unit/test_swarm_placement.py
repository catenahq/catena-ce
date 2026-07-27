"""Every swarm service that mounts state is pinned to the data node.

At one node a placement constraint is a no-op, which is exactly why this is
easy to get wrong and expensive to discover. The moment a second node joins,
swarm will reschedule a service carrying a node-LOCAL named volume onto the
new node and create a fresh EMPTY volume there. No error, no warning: the
container starts and the data is simply not there. That is the documented,
unsolved failure in Dokploy's multi-node story, and the constraint is what
makes `docker swarm join` a safe operation instead of a data-loss event.

Two structural properties, because a behavioural test would need a cluster:

  - the label roles/docker SETS and the label the constraints REFERENCE agree;
  - constraints live wholly in the desired-spec dict, never split between it
    and the hand-written create argv. swarm_service_drift reconciles the FULL
    constraint set, so a constraint passed only in the argv would be
    --constraint-rm'd on the very next converge -- silently unpinning the
    service it was protecting.

The cross-repo version of the first rule is `audit --check-swarm` in ops,
which fails the build when a service that mounts anything has no constraint.

Run: uv run pytest tests/unit/test_swarm_placement.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "roles"

# Services deployed by this repo that mount state, and the role that owns each.
STATEFUL = ("postgres", "portainer", "traefik")

DESIRED_VAR = {
    "postgres": "catena_postgres_desired",
    "portainer": "portainer_desired",
    "traefik": "traefik_desired",
}
CONSTRAINTS_VAR = {
    "postgres": "catena_postgres_constraints",
    "portainer": "portainer_constraints",
    "traefik": "traefik_constraints",
}


def _defaults(role: str) -> dict:
    return yaml.safe_load((ROLES / role / "defaults" / "main.yml").read_text())


def _tasks(role: str) -> list[dict]:
    return yaml.safe_load((ROLES / role / "tasks" / "main.yml").read_text())


def _create_argv(role: str) -> str:
    for task in _tasks(role):
        name = task.get("name") or ""
        if "swarm service (first run)" in name:
            return str(task["vars"])
    raise AssertionError(f"no create task found in roles/{role}")


def test_the_node_label_roles_docker_sets_matches_what_the_roles_constrain_to():
    """A shared variable would break a tag-scoped run (role defaults are only
    reliably in scope after that role has applied), so each role spells the
    constraint out. This is the check that keeps the copies honest."""
    label_value = _defaults("docker")["docker_node_role_label"]
    expected = f"node.labels.catena.role=={label_value}"
    for role in STATEFUL:
        constraints = _defaults(role)[CONSTRAINTS_VAR[role]]
        assert expected in constraints, (
            f"roles/{role} does not pin to the label roles/docker sets "
            f"({expected!r}); a worker could take its volume"
        )


@pytest.mark.parametrize("role", STATEFUL)
def test_constraints_are_wholly_owned_by_the_desired_dict(role: str):
    argv = _create_argv(role)
    assert "--constraint" not in argv, (
        f"roles/{role} passes a constraint in the create argv. "
        "swarm_service_drift reconciles the FULL constraint set, so the next "
        "converge would --constraint-rm it and unpin the service"
    )
    desired = _defaults(role)[DESIRED_VAR[role]]
    assert "constraints" in desired


@pytest.mark.parametrize("role", STATEFUL)
def test_every_mounting_service_is_constrained(role: str):
    """The rule the ops audit gate enforces workspace-wide, checked here for
    the three services this repo deploys directly."""
    argv = _create_argv(role)
    mounts_something = "--mount" in argv or "traefik_mounts" in argv
    assert mounts_something, (
        f"roles/{role} no longer mounts anything -- if that is deliberate the "
        "constraint may be droppable, but say so explicitly"
    )
    assert _defaults(role)[CONSTRAINTS_VAR[role]]


def test_socket_mounting_services_also_require_a_manager():
    """Portainer and Traefik bind-mount the docker socket and drive the swarm
    API, which only a manager can serve. Losing this would let them schedule
    onto a worker where the socket answers for nothing."""
    for role in ("portainer", "traefik"):
        assert "node.role==manager" in _defaults(role)[CONSTRAINTS_VAR[role]]


def test_postgres_is_not_pinned_to_a_manager():
    """It mounts no socket and needs no swarm API, so a data-labelled worker
    is a legitimate home for it. Over-constraining would rule that out for no
    reason."""
    assert "node.role==manager" not in _defaults("postgres")["catena_postgres_constraints"]
