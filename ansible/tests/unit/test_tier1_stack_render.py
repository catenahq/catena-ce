"""Unit tests for the tier-1 stack renderer.

The three swarm compose traps this pins were all found by a bench run
failing hours in, so each has a test rather than a comment:
`host_ip` is rejected outright, `restart:` and `depends_on` are SILENTLY
ignored. A silent no-op is worse than a rejection -- it reads as intent
to every future maintainer while doing nothing -- so the renderer refuses
to emit either and says why.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_MOD = Path(__file__).resolve().parents[2] / (
    "playbooks/filter_plugins/tier1_stack.py")
_spec = importlib.util.spec_from_file_location("tier1_stack", _MOD)
tier1_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tier1_stack)

render = tier1_stack.tier1_stack_render


def _pg(**over):
    spec = {
        "name": "catena-postgres",
        "image": "postgres:18",
        "networks": ["catena-network"],
        "secrets": ["catena-postgres-pw-ab12cd34"],
        "volumes": ["catena-postgres-data:/var/lib/postgresql/data"],
        "environment": {"POSTGRES_USER": "catena", "POSTGRES_DB": "postgres"},
        "limit_memory": "2g",
        "stop_grace_period": "30s",
        "update_failure_action": "pause",
        "health_cmd": "pg_isready -U catena",
        "health_interval": "10s",
        "health_retries": 5,
    }
    spec.update(over)
    return spec


# ── the three swarm traps ────────────────────────────────────────────


def test_host_ip_is_refused_because_swarm_rejects_the_file() -> None:
    """A swarm PortConfig has no host-IP field. Emitting it does not
    degrade -- `docker stack deploy` refuses the whole file, taking the
    entire control plane with it."""
    spec = _pg(ports=[{"target": 8080, "published": 18080,
                       "host_ip": "127.0.0.1"}])
    with pytest.raises(ValueError, match="host_ip"):
        render([spec])


def test_depends_on_is_refused_because_swarm_ignores_it() -> None:
    with pytest.raises(ValueError, match="depends_on"):
        render([_pg(depends_on=["catena-traefik"])])


def test_top_level_restart_is_refused_because_swarm_ignores_it() -> None:
    with pytest.raises(ValueError, match="restart"):
        render([_pg(restart="always")])


# ── ordering is restart_policy, and delay is load-bearing ────────────


def test_every_service_gets_a_restart_policy_with_a_delay() -> None:
    """Ordering in swarm IS crash-and-retry. Without `delay` a service
    whose dependency is not up yet hot-loops."""
    out = render([_pg()])
    policy = out["services"]["catena-postgres"]["deploy"]["restart_policy"]
    assert policy == {"condition": "any", "delay": tier1_stack.RESTART_DELAY}


def test_ports_render_long_syntax_host_mode() -> None:
    """Short syntax would publish on the INGRESS mesh, where the host
    firewall does not apply."""
    out = render([_pg(ports=[{"target": 8080, "published": 18080}])])
    assert out["services"]["catena-postgres"]["ports"] == [
        {"target": 8080, "published": 18080, "protocol": "tcp", "mode": "host"}
    ]


# ── the rollback carve-out ───────────────────────────────────────────


def test_update_failure_action_rollback_is_refused() -> None:
    """swarm rolling back underneath the managed-update engine either
    re-applies the bad image or records a version that never shipped as
    bumped -- `--rollback` reverts to the previous spec, which after an
    auto-rollback IS the failed one."""
    with pytest.raises(ValueError, match="rollback"):
        render([_pg(update_failure_action="rollback")])


def test_update_failure_action_pause_passes_through() -> None:
    out = render([_pg()])
    cfg = out["services"]["catena-postgres"]["deploy"]["update_config"]
    assert cfg["failure_action"] == "pause"


# ── externals: the stack references, never creates ───────────────────


def test_networks_volumes_and_secrets_are_declared_external() -> None:
    """Named volumes carry data that predates the render and secrets are
    content-hashed by the rotation path. A stack that CREATED them would
    take ownership of state it cannot safely recreate."""
    out = render([_pg()])
    assert out["networks"] == {"catena-network": {"external": True}}
    assert out["volumes"] == {"catena-postgres-data": {"external": True}}
    assert out["secrets"] == {
        "catena-postgres-pw-ab12cd34": {"external": True}}


def test_bind_mounts_are_not_declared_as_named_volumes() -> None:
    """A bind source is a host path, not a volume; declaring it external
    makes swarm look for a volume that does not exist."""
    out = render([_pg(volumes=["/etc/catena/traefik:/etc/traefik:ro"])])
    assert "volumes" not in out


# ── global mode ──────────────────────────────────────────────────────


def test_global_mode_drops_replicas() -> None:
    """swarm rejects a global service that also declares replicas."""
    deploy = render([_pg(mode="global")])["services"]["catena-postgres"]["deploy"]
    assert deploy["mode"] == "global"
    assert "replicas" not in deploy


# ── shape ────────────────────────────────────────────────────────────


def test_container_labels_land_on_the_container_not_the_service() -> None:
    """gatus-sync reads `docker ps`, which reports labels on the task
    container; deploy.labels would put them where nothing looks."""
    out = render([_pg(container_labels={"vps.auth.mode": "private"})])
    svc = out["services"]["catena-postgres"]
    assert svc["labels"] == {"vps.auth.mode": "private"}
    assert "labels" not in svc["deploy"]


def test_healthcheck_uses_cmd_shell() -> None:
    hc = render([_pg()])["services"]["catena-postgres"]["healthcheck"]
    assert hc["test"] == ["CMD-SHELL", "pg_isready -U catena"]
    assert hc["retries"] == 5


def test_missing_image_is_refused() -> None:
    with pytest.raises(ValueError, match="image"):
        render([{"name": "x"}])


def test_duplicate_service_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        render([_pg(), _pg()])


def test_empty_service_list_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        render([])


def test_whole_control_plane_renders_together() -> None:
    """The blast-radius case: five services in one file. A render error in
    any one of them must surface HERE, before an apply takes traefik,
    postgres and portainer down together."""
    services = [
        _pg(),
        {"name": "catena-traefik", "image": "traefik:v3.7.7",
         "networks": ["catena-network"],
         "volumes": ["/etc/catena/traefik:/etc/traefik:ro"],
         "ports": [{"target": 443, "published": 443}]},
        {"name": "catena-portainer", "image": "portainer/portainer-ce:2.43.0",
         "networks": ["catena-network"],
         "volumes": ["catena-portainer-data:/data"]},
        {"name": "coturn", "image": "coturn/coturn:4.10.0-alpine",
         "mode": "global"},
        {"name": "catena-admin", "image": "ghcr.io/catenahq/catena-admin:1",
         "networks": ["catena-network"],
         "extra_hosts": ["host.docker.internal:host-gateway"]},
    ]
    out = render(services)
    assert set(out["services"]) == {
        "catena-postgres", "catena-traefik", "catena-portainer",
        "coturn", "catena-admin",
    }
    assert out["services"]["catena-admin"]["extra_hosts"] == [
        "host.docker.internal:host-gateway"]
    # Every service carries a restart policy; that IS the ordering model.
    for svc in out["services"].values():
        assert svc["deploy"]["restart_policy"]["condition"] == "any"
