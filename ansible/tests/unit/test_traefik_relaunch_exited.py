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


def test_it_is_a_swarm_service_whose_spec_comes_from_the_engine():
    """The role no longer writes a create argv, and no longer builds a spec
    either: it ASKS the catena-tier1 host engine for one and hands that to
    reconcile_one.yml.

    What the constraint IS -- node.role==manager, because it bind-mounts the
    docker socket and drives the swarm provider -- is asserted in the engine,
    catena-admin payload/engines/tier1/catalog_test.go, against the same
    declaration this role used to build. Re-asserting it here would mean
    reading a dict that no longer drives anything."""
    _find("read the built-in tier-1 spec from the host engine")
    adopt = str(_find("adopt the built-in spec")["ansible.builtin.set_fact"])
    assert "tier1_builtin_spec" in adopt, (
        "the role adopts something other than the engine's answer"
    )
    assert "traefik_desired" not in _code(TASKS), (
        "the role is back to declaring its own spec, which puts a second "
        "description of catena-traefik beside the engine's"
    )


def test_no_host_ports_is_structural_not_checked():
    """cloudflared over the overlay is the only ingress. Nothing in this role
    can add a publish: the spec comes from the engine, which declares no
    `ports` for traefik (asserted there), and the role adds nothing on top."""
    assert "--publish" not in _code(TASKS)
    assert "PortBindings" not in _code(TASKS)
    assert "ports" not in str(_find("adopt the built-in spec")["ansible.builtin.set_fact"])


def test_static_config_change_forces_exactly_one_roll():
    """Traefik reads traefik.yml once at startup -- only the FILE PROVIDER
    hot-reloads, and that covers dynamic/, not this. So a static config change
    has to roll the task. But when the spec reconcile already ran it rolled
    the task itself, and forcing a second roll would drop ingress twice.

    The signal is now the catena-tier1 engine's own summary line, registered
    as _t1_apply by the shared dispatch -- which is exactly why this test
    matters more than it did: the variable the gate reads is not written by
    this role, and its default has to be the one that rolls (a redundant roll
    beats a static config the running task never picks up)."""
    task = _find("force-roll to pick up a static config")
    assert "--force" in task["ansible.builtin.command"]["argv"]
    gate = next(c for c in task["when"] if "_t1_apply" in str(c))
    assert "changed=0" in gate
    assert "default('changed=0')" in gate


def test_the_healthcheck_has_its_precondition():
    """`traefik healthcheck` exits 1 with "please enable `ping`" unless the
    static config turns the endpoint on, so the health command without the ping
    block would mark every task unhealthy.

    The two halves of that coupling now live in different repos: the engine
    ships the health command (catena-admin catalog.go, asserted there), and this
    repo renders the static config that has to enable the endpoint it calls.
    This is the half this repo owns, and it is the half that can silently
    regress -- deleting one line from a Jinja template is easier than noticing
    that a probe in another repo depends on it."""
    assert "ping: {}" in STATIC_CFG.read_text(), (
        "traefik.yml.j2 no longer enables the ping endpoint, but the tier-1 "
        "engine still probes catena-traefik with `traefik healthcheck`; every "
        "task would report unhealthy"
    )
