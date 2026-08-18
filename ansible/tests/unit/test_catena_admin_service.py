"""The catena-admin swarm-service argv, its env and its secrets.

The panel is created with `docker service create` rather than through
Portainer's stack API, so the shape of that argv IS the deployment. Two
consumers render it -- the converge and the test bench -- which is why the
mounts and labels live in the plugin and not in either caller.

Run: uv run pytest tests/unit/test_catena_admin_service.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PLUGIN = (
    Path(__file__).resolve().parents[2]
    / "playbooks" / "filter_plugins" / "catena_admin_service.py"
)


def _plugin():
    spec = importlib.util.spec_from_file_location("catena_admin_service", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spec(**over):
    base = {
        "name": "catena-admin",
        "image": "ghcr.io/catenahq/catena-admin:latest",
        "network": "catena-network",
        "ui_port": "9010",
        "direct_port": "8001",
        "group": "1000",
        "env": {"KEYCLOAK_REALM": "vps"},
        "secrets": [],
        "hardening": [],
    }
    base.update(over)
    return base


# ─── argv ────────────────────────────────────────────────────────────────


def test_the_image_is_last_and_carries_no_command():
    argv = _plugin().catena_admin_service_argv(_spec())
    assert argv[-1] == "ghcr.io/catenahq/catena-admin:latest"
    assert argv[:3] == ["docker", "service", "create"]


def test_the_create_does_not_block_on_convergence():
    """Without --detach the CLI waits for the service to converge, and
    --restart-condition=any means a task that cannot start is retried forever
    -- so the wait never returns and the install parks here with no output.
    The play polls for Running instead, bounded, and can name the task error."""
    argv = _plugin().catena_admin_service_argv(_spec())
    assert "--detach" in argv


def test_the_port_is_published_host_mode():
    # An ingress publish would put the native-login listener on the routing
    # mesh, where the public-ports registry's tailnet firewall does not
    # apply -- the panel's login page would answer from the public IP.
    argv = _plugin().catena_admin_service_argv(_spec())
    assert "--publish=mode=host,published=9010,target=8001" in argv


def test_the_host_gateway_alias_is_set():
    # The SSH dispatch resolves the host through this name; without it every
    # host Action fails to connect and the panel renders but does nothing.
    argv = _plugin().catena_admin_service_argv(_spec())
    assert "--host=host.docker.internal:host-gateway" in argv


def test_labels_are_container_labels_not_service_labels():
    # gatus-sync reads `docker ps`, which reports the TASK container's
    # labels. As --label these land on the service object, where nothing
    # looks, and the panel drops off the monitored set silently.
    argv = _plugin().catena_admin_service_argv(_spec())
    assert "--container-label=vps.auth.mode=private" in argv
    assert not any(a.startswith("--label=") for a in argv)


def test_the_payload_mount_stays_writable_inside_the_read_only_parent():
    argv = _plugin().catena_admin_service_argv(_spec())
    assert ("--mount=type=bind,source=/var/lib/catena/ee-payload,"
            "destination=/var/lib/catena/ee-payload") in argv
    assert ("--mount=type=bind,source=/var/lib/catena,"
            "destination=/var/lib/catena,readonly") in argv


def test_the_ssh_key_mount_is_read_only():
    argv = _plugin().catena_admin_service_argv(_spec())
    assert ("--mount=type=bind,source=/etc/catena/admin-ssh,"
            "destination=/etc/catena/admin-ssh,readonly") in argv


def test_a_missing_required_field_is_refused():
    with pytest.raises(ValueError):
        _plugin().catena_admin_service_argv(_spec(image=""))


# ─── secrets ─────────────────────────────────────────────────────────────


def test_a_secret_is_named_after_its_value():
    # Swarm secrets are immutable. A fixed name would keep mounting the
    # original bytes after a rotation, with nothing reporting it.
    p = _plugin()
    one = p.secret_entries([{"base": "k", "target": "K", "value": "first"}])
    two = p.secret_entries([{"base": "k", "target": "K", "value": "second"}])
    assert one[0]["name"] != two[0]["name"]
    assert one[0]["name"].startswith("k-")


def test_a_blank_secret_is_dropped_rather_than_stored():
    # An empty secret file is not a credential, and mounting one makes the
    # shell read empty instead of falling through to its documented env
    # fallback.
    p = _plugin()
    assert p.secret_entries([{"base": "k", "target": "K", "value": "  "}]) == []


def test_every_secret_gets_its_file_env_var():
    # The failure this prevents is invisible: the credential is mounted, the
    # container starts, and the feature it drives is silently off because
    # nothing told the shell where to read.
    p = _plugin()
    env = p.catena_admin_full_env(_spec(secrets=[
        {"name": "catena_admin_session_key-abc12345",
         "target": "CATENA_ADMIN_SESSION_KEY"},
    ]))
    assert env["CATENA_ADMIN_SESSION_KEY_FILE"] == \
        "/run/secrets/CATENA_ADMIN_SESSION_KEY"


def test_the_secret_value_never_reaches_the_argv():
    # The whole reason secrets exist here: an --env value is readable in the
    # host process table by any local user for the duration of the create.
    p = _plugin()
    entries = p.secret_entries([
        {"base": "catena_admin_session_key",
         "target": "CATENA_ADMIN_SESSION_KEY", "value": "sk_supersecret"},
    ])
    argv = p.catena_admin_service_argv(_spec(secrets=entries))
    assert not any("sk_supersecret" in a for a in argv)
    assert f"--secret=source={entries[0]['name']}," \
           f"target=CATENA_ADMIN_SESSION_KEY" in argv


# ─── drift ───────────────────────────────────────────────────────────────


def _live(env=None, secrets=None):
    return [{"Spec": {"TaskTemplate": {"ContainerSpec": {
        "Env": env or [],
        "Secrets": secrets or [],
    }}}}]


def test_a_converged_service_reports_no_env_drift():
    # Every env update recreates the task. Reporting drift where there is
    # none would restart the panel on every converge -- unavailable exactly
    # while a converge runs.
    p = _plugin()
    assert p.catena_admin_env_drift(
        _live(env=["KEYCLOAK_REALM=vps"]), {"KEYCLOAK_REALM": "vps"}) == []


def test_a_changed_value_is_added_and_a_dropped_key_removed():
    p = _plugin()
    args = p.catena_admin_env_drift(
        _live(env=["KEYCLOAK_REALM=old", "GONE=1"]),
        {"KEYCLOAK_REALM": "vps"},
    )
    assert args == ["--env-add", "KEYCLOAK_REALM=vps", "--env-rm", "GONE"]


def test_a_rotated_secret_is_swapped_not_left_behind():
    p = _plugin()
    args = p.catena_admin_secret_drift(
        _live(secrets=[{"SecretName": "k-old11111",
                        "File": {"Name": "K"}}]),
        [{"name": "k-new22222", "target": "K"}],
    )
    assert args == [
        "--secret-rm", "k-old11111",
        "--secret-add", "source=k-new22222,target=K",
    ]


def test_an_unchanged_secret_reports_no_drift():
    p = _plugin()
    assert p.catena_admin_secret_drift(
        _live(secrets=[{"SecretName": "k-abc12345", "File": {"Name": "K"}}]),
        [{"name": "k-abc12345", "target": "K"}],
    ) == []
