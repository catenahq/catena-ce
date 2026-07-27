"""catena-traefik stays a swarm service, and the plain-container shape stays out.

A plain container attaches to catena-network by network ID. When the overlay
is torn down and recreated -- a restore rebuilds swarm+overlay from scratch,
and an interrupted converge or daemon flap does the same -- the ID it holds is
stale: `docker start` fails with "network <old-id> not found" and it sits
Exited with ingress dead. A swarm service rejoins by NAME, so the task manager
re-dispatches it and the failure cannot occur.

That is why these are structural assertions about the SHAPE of the role rather
than assertions about a repair mechanism. There is no repair mechanism to
test; the shape is what removes the need for one.

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
    """The file with comment lines stripped, so a comment naming the
    plain-container shape in order to rule it out does not read as the thing."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_role_runs_no_plain_container():
    body = _code(TASKS)
    for forbidden in ("docker run", "_traefik_needs_relaunch",
                      ".State.Running", "docker restart"):
        assert forbidden not in body, (
            f"{forbidden!r} is in roles/traefik: a plain container on the "
            "catena-network overlay carries the stale-network-ID failure that "
            "strands ingress on every daemon restart"
        )
    # Exactly one `docker rm -f`: the guard that clears a plain container
    # squatting on the service name (gated in the test below). A second would
    # mean a rm-and-relaunch lifecycle.
    assert body.count("docker rm -f") == 1


def test_the_plain_container_removal_is_gated_on_the_service_being_absent():
    """A plain container by this name holds the same config mounts and the
    same overlay attachment, so it must go before the service starts. Once
    the service exists there is nothing to find, and an ungated removal would
    tear down a swarm task container by name."""
    task = _find("remove a plain container holding the name")
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
    """cloudflared over the overlay is the only ingress. The create argv has
    no --publish, so there is no port binding for a drift check to find."""
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
