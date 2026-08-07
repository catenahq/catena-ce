"""The node label this repo SETS is the one the constraints REFERENCE.

At one node a placement constraint is a no-op, which is exactly why this is
easy to get wrong and expensive to discover. The moment a second node joins,
swarm will reschedule a service carrying a node-LOCAL named volume onto the
new node and create a fresh EMPTY volume there. No error, no warning: the
container starts and the data is simply not there. That is the documented,
unsolved failure in Dokploy's multi-node story, and the constraint is what
makes `docker swarm join` a safe operation instead of a data-loss event.

WHAT IS LEFT HERE. Every tier-1 constraint moved into the host engine
(catena-admin payload/engines/tier1/catalog.go) as roles/postgres, roles/traefik,
roles/portainer and roles/coturn stopped declaring dicts and started asking
`catena-tier1 spec` for them. The constraints are asserted there, in
catalog_test.go: every mounting service pinned to the data node, every
socket-mounting service also requiring a manager, and postgres deliberately not
requiring one.

Asserting them here too would mean reading dicts that no longer drive anything
-- the stale-oracle failure this suite exists to catch in other people's code.

What this repo still owns is the OTHER half of the string: roles/docker sets
the label the engine's constraints select on. If the two disagree, every
constrained service becomes unschedulable and the failure is a pending task
with no explanation. The cross-repo version of this check is
`audit --check-swarm` in ops, which walks the workspace; this is the local
guard on the value.

Run: uv run pytest tests/unit/test_swarm_placement.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "roles"

# The value the engine's constraints spell as
# "node.labels.catena.role==<value>". Hardcoded rather than imported: the
# engine is in a sibling repo, and a test that reads across the workspace
# passes on a laptop and cannot run in CI. `audit --check-swarm` is the check
# that actually spans the two.
ENGINE_CONSTRAINT_LABEL_VALUE = "data"


def _defaults(role: str) -> dict:
    return yaml.safe_load((ROLES / role / "defaults" / "main.yml").read_text())


def test_the_node_label_roles_docker_sets_is_the_one_the_engine_constrains_to():
    label_value = _defaults("docker")["docker_node_role_label"]
    assert label_value == ENGINE_CONSTRAINT_LABEL_VALUE, (
        f"roles/docker labels the data node {label_value!r}, but the tier-1 "
        f"engine constrains to node.labels.catena.role=={ENGINE_CONSTRAINT_LABEL_VALUE}. "
        "Every constrained service would be unschedulable, showing up as a "
        "pending task with no explanation"
    )


def test_the_label_is_applied_to_the_node_this_repo_converges():
    """A label the engine selects on and nothing ever sets is the same failure
    as a mismatched one, and reads as a converge that simply did nothing."""
    text = (ROLES / "docker" / "tasks" / "main.yml").read_text()
    assert "docker_node_role_label" in text, (
        "roles/docker declares the label but never applies it to the node"
    )
