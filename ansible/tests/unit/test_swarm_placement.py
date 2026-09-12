"""The node label this repo SETS is the one the constraints REFERENCE.

At one node a placement constraint is a no-op, which is exactly why this is
easy to get wrong and expensive to discover. The moment a second node joins,
swarm will reschedule a service carrying a node-LOCAL named volume onto the
new node and create a fresh EMPTY volume there. No error, no warning: the
container starts and the data is simply not there. That is the documented,
unsolved failure in Dokploy's multi-node story, and the constraint is what
makes `docker swarm join` a safe operation instead of a data-loss event.

WHAT THIS FILE COVERS. The tier-1 constraints live in the host engine
(catena-admin payload/engines/tier1/catalog.go), which reconcile/roles/postgres,
reconcile/roles/traefik, reconcile/roles/portainer and reconcile/roles/coturn ask
with `catena-tier1 spec`. They are asserted there, in catalog_test.go: every
mounting service pinned to the data node, every socket-mounting service also
requiring a manager, and postgres deliberately not requiring one.

Restating them here would mean reading dicts this repo does not feed to anything
-- the stale-oracle failure this suite exists to catch in other people's code.

What this repo still owns is the OTHER half of the string: bootstrap/roles/docker sets
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
_ROLE_ROOTS = (ANSIBLE / "bootstrap" / "roles",
               ANSIBLE / "reconcile" / "roles")

def _role_dir(name: str) -> Path:
    """Where a role lives, whichever side it is on.

    Phase 1b split roles/ into bootstrap/roles/ and reconcile/roles/. Resolved
    by search rather than by a hard-coded side so a role moving across the line
    -- which is a thing this migration does -- does not need this file edited
    too.
    """
    for root in _ROLE_ROOTS:
        if (root / name).is_dir():
            return root / name
    raise AssertionError(f"no role named {name} under {[str(r) for r in _ROLE_ROOTS]}")


# The value the engine's constraints spell as
# "node.labels.catena.role==<value>". Hardcoded rather than imported: the
# engine is in a sibling repo, and a test that reads across the workspace
# passes on a laptop and cannot run in CI. `audit --check-swarm` is the check
# that actually spans the two.
ENGINE_CONSTRAINT_LABEL_VALUE = "data"


def _defaults(role: str) -> dict:
    return yaml.safe_load(
        (_role_dir(role) / "defaults" / "main.yml").read_text())


def test_the_node_label_roles_docker_sets_is_the_one_the_engine_constrains_to():
    label_value = _defaults("docker")["docker_node_role_label"]
    assert label_value == ENGINE_CONSTRAINT_LABEL_VALUE, (
        f"bootstrap/roles/docker labels the data node {label_value!r}, but the tier-1 "
        f"engine constrains to node.labels.catena.role=={ENGINE_CONSTRAINT_LABEL_VALUE}. "
        "Every constrained service would be unschedulable, showing up as a "
        "pending task with no explanation"
    )


def test_the_label_is_applied_to_the_node_this_repo_converges():
    """A label the engine selects on and nothing ever sets is the same failure
    as a mismatched one, and reads as a converge that simply did nothing."""
    text = (_role_dir("docker") / "tasks" / "main.yml").read_text()
    assert "docker_node_role_label" in text, (
        "bootstrap/roles/docker declares the label but never applies it to the node"
    )
