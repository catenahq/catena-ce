"""Unit tests for the swarm-service compose renderer -- the ORACLE.

The renderer does not apply anything. What creates and reconciles these
services is the catena-admin host engine (payload/engines/tier1); this file is
rendered and handed to `docker stack config` on every converge so a spec docker
would reject is caught before the engine tries to create from it.

The swarm compose traps pinned here each cost a bench run failing hours in,
so each has a test rather than a comment. Two kinds:

  REJECTED by `docker stack config` -- `host_ip`, `group_add`. The file
  does not load at all, so the whole control plane goes down together.
  SILENTLY IGNORED -- `restart:`, `depends_on`, `network_mode:`. Worse: it
  reads as intent to every future maintainer while doing nothing.

The renderer refuses both kinds and says why. Every claim here was checked
against a real `docker stack config`; the converge re-checks the rendered
file the same way on the host, so this file's memory of the schema is never
the only thing standing between a bad render and the control plane.
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
mount = tier1_stack.swarm_mount_to_compose
upsert = tier1_stack.tier1_spec_upsert


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


def test_network_mode_is_refused_because_swarm_ignores_it() -> None:
    """Host networking in a stack is membership in the external network
    literally named `host`, not network_mode."""
    with pytest.raises(ValueError, match="network_mode"):
        render([_pg(network_mode="host")])


def test_a_supplementary_group_is_refused_because_the_schema_has_no_slot()\
        -> None:
    """`docker stack config` answers "Additional property group_add is not
    allowed" under both the versioned and version-less loader, and the
    compose schema has no other spelling. A service that needs one keeps
    its own create argv -- which is why catena-admin is not in this stack."""
    for key in ("group", "group_add"):
        with pytest.raises(ValueError, match="group"):
            render([_pg(**{key: "998"})])


def test_host_networking_renders_as_an_external_network_named_host() -> None:
    out = render([_pg(networks=["host"])])
    assert out["services"]["catena-postgres"]["networks"] == ["host"]
    assert out["networks"] == {"host": {"external": True}}


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


def test_stop_grace_period_survives_the_render() -> None:
    """It is in every *_desired dict and swarm_service_create_args emits it;
    dropping it here would silently shorten every drain to the 10s default."""
    out = render([_pg()])
    assert out["services"]["catena-postgres"]["stop_grace_period"] == "30s"


def test_no_version_key_is_emitted() -> None:
    """The loader stamps its own and calls a declared one obsolete."""
    assert "version" not in render([_pg()])


# ── secrets: two spellings, one of them load-bearing for rotation ────


def test_a_content_hashed_secret_renders_source_and_target() -> None:
    """The SOURCE carries the value's hash and changes on rotation; the
    TARGET is the stable filename the container reads. Collapsing them
    would make a rotation change the path the shell reads."""
    spec = _pg(secrets=[{"name": "catena-admin-sess-ab12cd34",
                         "target": "CATENA_ADMIN_SESSION_KEY"}])
    out = render([spec])
    assert out["services"]["catena-postgres"]["secrets"] == [
        {"source": "catena-admin-sess-ab12cd34",
         "target": "CATENA_ADMIN_SESSION_KEY"}]
    # The top-level declaration keys off the SOURCE, not the target.
    assert out["secrets"] == {
        "catena-admin-sess-ab12cd34": {"external": True}}


def test_a_secret_without_a_target_is_refused() -> None:
    with pytest.raises(ValueError, match="target"):
        render([_pg(secrets=[{"name": "x"}])])


# ── --mount argv -> compose volumes ─────────────────────────────────


def test_mount_accepts_both_destination_and_target_spellings() -> None:
    """The roles use both: traefik/postgres/portainer say destination=,
    coturn says target=. Docker accepts either, so this must too."""
    assert mount(
        "type=bind,source=/etc/catena/traefik,destination=/etc/traefik"
    ) == "/etc/catena/traefik:/etc/traefik"
    assert mount(
        "type=bind,source=/etc/coturn/turnserver.conf,"
        "target=/etc/coturn/turnserver.conf,readonly"
    ) == "/etc/coturn/turnserver.conf:/etc/coturn/turnserver.conf:ro"


def test_mount_carries_a_named_volume_through() -> None:
    assert mount(
        "type=volume,source=catena-postgres-data,"
        "destination=/var/lib/postgresql/data"
    ) == "catena-postgres-data:/var/lib/postgresql/data"


def test_mount_without_a_destination_is_refused() -> None:
    with pytest.raises(ValueError, match="source/destination"):
        mount("type=bind,source=/etc/catena")


def test_detach_does_not_leak_into_the_compose_service() -> None:
    """It is a property of the engine's create call, not of the service.
    Compose has no equivalent, so the render drops it rather than inventing
    one."""
    svc = render([_pg(detach=True)])["services"]["catena-postgres"]
    assert "detach" not in svc
    assert "detach" not in svc["deploy"]


# ── accumulating the specs across roles ─────────────────────────────


def test_upsert_replaces_a_same_named_spec_rather_than_appending() -> None:
    """site.yml re-runs coturn in post_tasks: the post-restore hooks bring
    the TURN consumer up AFTER reconcile/roles/coturn probed for one and correctly
    found none. Appending would hand the renderer a duplicate name and fail
    the converge on the recovery path specifically."""
    first = {"name": "coturn", "image": "coturn/coturn:4.10.0-alpine"}
    second = {"name": "coturn", "image": "coturn/coturn:4.11.0-alpine"}
    out = upsert(upsert([_pg()], first), second)
    assert [s["name"] for s in out] == ["catena-postgres", "coturn"]
    assert out[-1]["image"] == "coturn/coturn:4.11.0-alpine"


def test_upsert_seeds_an_undefined_accumulator() -> None:
    assert upsert(None, {"name": "a", "image": "x"}) == [
        {"name": "a", "image": "x"}]


def test_upsert_refuses_a_nameless_spec() -> None:
    with pytest.raises(ValueError, match="name"):
        upsert([], {"image": "x"})


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
    """The blast-radius case: the control-plane set in one file. A render error
    in any one of them must surface HERE, before the engine takes traefik,
    postgres and portainer down together.

    catena-admin is deliberately absent: it needs a supplementary group,
    which `docker stack config` rejects, so it keeps its own create argv."""
    services = [
        _pg(),
        {"name": "catena-traefik", "image": "traefik:v3.7.7",
         "networks": ["catena-network"],
         "volumes": ["/etc/catena/traefik:/etc/traefik:ro"],
         "constraints": ["node.role==manager"]},
        {"name": "catena-portainer", "image": "portainer/portainer-ce:2.43.0",
         "networks": ["catena-network"],
         "secrets": ["catena_portainer_admin_password"],
         "volumes": ["/var/run/docker.sock:/var/run/docker.sock",
                     "catena-portainer-data:/data"],
         "ports": [{"target": 9000, "published": 9443}],
         "command": ["--admin-password-file=/run/secrets/"
                     "catena_portainer_admin_password"]},
    ]
    out = render(services)
    assert set(out["services"]) == {
        "catena-postgres", "catena-traefik", "catena-portainer",
    }
    assert set(out["networks"]) == {"catena-network"}
    # Only the two NAMED volumes; the two bind sources are host paths.
    assert set(out["volumes"]) == {
        "catena-postgres-data", "catena-portainer-data"}
    # Every service carries a restart policy; that IS the ordering model.
    for svc in out["services"].values():
        assert svc["deploy"]["restart_policy"]["condition"] == "any"


def test_a_companion_renders_through_the_same_renderer() -> None:
    """coturn renders into companions.yml rather than tier1.yml -- it is
    presence-gated on a Talk or Jitsi consumer, and mixing it in made the
    control-plane file change on a converge where the control plane had not.
    Same renderer either way, so the traps above still hold for it: host
    networking, global mode, and a placement constraint pinning it to the node
    that holds the bind sources (a bind whose source is missing is CREATED
    empty)."""
    out = render([
        {"name": "coturn", "image": "coturn/coturn:4.10.0-alpine",
         "mode": "global", "networks": ["host"], "user": "0:0",
         "constraints": ["node.labels.catena.role==data"],
         "volumes": ["/etc/coturn/turnserver.conf:"
                     "/etc/coturn/turnserver.conf:ro"],
         "command": ["-c", "/etc/coturn/turnserver.conf"]},
    ])
    svc = out["services"]["coturn"]
    assert svc["deploy"]["mode"] == "global"
    assert "replicas" not in svc["deploy"]
    assert out["networks"] == {"host": {"external": True}}
    # A bind source is a host path, so no named volume is declared for it.
    assert "volumes" not in out
