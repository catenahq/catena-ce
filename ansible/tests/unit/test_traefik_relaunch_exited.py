"""Lock the property the traefik relaunch self-heal used to provide.

catena-traefik WAS a plain container that attached to catena-network by
network ID. When the overlay was torn down and recreated (a restore rebuilds
swarm+overlay; an interrupted converge or daemon flap does the same), the new
overlay had a fresh ID and the container's baked-in reference went stale:
`docker start` failed with "network <old-id> not found" and it sat Exited with
ingress dead. This file used to pin the compensating machinery -- read
`.State.Running`, OR it into a `_traefik_needs_relaunch` fact alongside the
image/ports/acme conditions, then rm + run.

The swarm conversion removed the failure instead of compensating for it: a
swarm service rejoins the overlay by NAME, so the task manager re-dispatches
it and the staleness cannot happen. All of that machinery is gone.

What is pinned now is that it does not come back, plus the two properties the
conversion has to keep: no host ports, and one roll per converge.

Run: uv run pytest tests/unit/test_traefik_relaunch_exited.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "traefik"
)
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
STATIC_CFG = ROLE / "templates" / "traefik.yml.j2"


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for t in _tasks():
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {name_fragment!r}")


def _code(path: Path) -> str:
    """The file with comment lines stripped, so a comment naming the retired
    plain-container shape in order to rule it out does not read as the thing."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_plain_container_shape_is_gone():
    body = _code(TASKS)
    for retired in ("docker run", "_traefik_needs_relaunch",
                    ".State.Running", "docker restart"):
        assert retired not in body, (
            f"{retired!r} is back in roles/traefik: the plain-container shape "
            "brings the stale-overlay-ID failure back with it"
        )
    # `docker rm -f` survives exactly once, for the one-time cutover removal
    # of the pre-swarm container (gated in the test below). A second one would
    # mean the rm-and-relaunch lifecycle came back.
    assert body.count("docker rm -f") == 1


def test_the_cutover_removal_is_the_only_rm_and_is_gated():
    """A host converging for the first time after the swarm change still has
    the old plain container holding the config mounts. Removing it must be
    gated on the service NOT existing, or a later converge would tear down a
    container that is not there and could not be re-created."""
    task = _find("remove the pre-swarm plain container")
    assert task["when"] == [
        "_traefik_plain.rc == 0",
        "_traefik_service.rc != 0",
    ]


def test_it_is_a_swarm_service_constrained_to_a_manager():
    argv = _find("create catena-traefik swarm service")["vars"]["_traefik_create_argv"]
    assert "'docker', 'service', 'create'" in argv
    # It bind-mounts the docker socket and drives the swarm provider, so it
    # cannot be scheduled onto a worker. The constraint lives in the desired
    # dict rather than the argv, because swarm_service_drift reconciles the
    # FULL constraint set and would --constraint-rm anything the dict omits;
    # test_swarm_placement.py owns that rule for all three services.
    constraints = yaml.safe_load(DEFAULTS.read_text())["traefik_constraints"]
    assert "node.role==manager" in constraints


def test_no_host_ports_is_structural_not_checked():
    """The old role read HostConfig.PortBindings and relaunched if non-empty.
    The create argv simply has no --publish, so there is nothing to check."""
    argv = _find("create catena-traefik swarm service")["vars"]["_traefik_create_argv"]
    assert "--publish" not in argv
    assert "PortBindings" not in _code(TASKS)


def test_static_config_change_forces_exactly_one_roll():
    """Traefik reads traefik.yml once at startup -- only the FILE PROVIDER
    hot-reloads, and that covers dynamic/, not this. So a static config change
    has to roll the task. But when the spec reconcile already ran it rolled
    the task itself, and forcing a second roll would drop ingress twice."""
    task = _find("force-roll to pick up a static config")
    assert "--force" in task["ansible.builtin.command"]["argv"]
    assert "_traefik_drift | default([]) | length == 0" in task["when"]


def test_the_healthcheck_has_its_precondition():
    """`traefik healthcheck` exits 1 with "please enable `ping`" unless the
    static config turns the endpoint on, so shipping the health command
    without the ping block would mark every task unhealthy."""
    assert "traefik healthcheck" in DEFAULTS.read_text()
    assert "ping: {}" in STATIC_CFG.read_text()
