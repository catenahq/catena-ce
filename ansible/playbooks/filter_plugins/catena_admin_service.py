"""Ansible filters: the catena-admin panel as a tier-1 swarm service, not a
Portainer stack.

The panel is the tool that repairs the host, the restore that brings Portainer
back included, so deploying it VIA Portainer would let a Portainer that will
not start take down the only tool that could repair it. The converge creates
the service directly instead, the same way reconcile/roles/traefik,
reconcile/roles/postgres and reconcile/roles/portainer do.

Four filters over one spec dict:

    catena_admin_service_argv(spec)          -> `docker service create` argv
    catena_admin_env_drift(inspect, env)     -> --env-add / --env-rm flags
    catena_admin_secret_drift(inspect, ss)   -> --secret-add / --secret-rm flags
    catena_admin_network_drift(inspect, ids) -> --network-add / --network-rm flags

WHY A SHARED RENDERER. Two consumers deploy this container and must agree
on its shape: the converge (reconcile/roles/catena-admin/tasks/deploy.yml), and
the test bench (ops automation/test_bench/orchestrator/catena_admin_deploy.py),
which recreates the panel from its run's image on a host a scenario left
without it. Both create the service directly against the local swarm,
with no stack API between them to hold a shared compose file, so the only thing
that can keep their argv identical is a renderer they both call. What MUST NOT
drift is exactly what nobody reads: the eight mounts. So the mounts, the
publish mode and the host-gateway alias live HERE as product facts, and the
spec carries only what is genuinely per-host.

The bench imports this module by path, the way it already reads role files
out of the catena-ce tree.

SECRETS, NOT --env. `docker service create` has no --env-file, so every
value passed with --env sits in the host's process table for the duration of
the create -- readable there by any local user, not just root. Several of the
panel's values are credentials. They arrive as swarm secrets mounted at
/run/secrets/<name>, and only the PATH travels in the environment, as
<KEY>_FILE. The reader is catena-admin shell/cmd/catena-admin/secretenv.go.

Swarm secrets are IMMUTABLE: a name, once created, is bound to its bytes
forever. So a rotated credential must arrive under a NEW name or it never
reaches the container, which is why secret_name() hashes the VALUE into the
name. A fixed name would make rotation a silent no-op. secret_entries, the
swarm_secret_entries filter, names every swarm secret the converge creates
(playbooks/tasks/swarm_secrets.yml), Healthchecks' too, and secrets_unmounted,
the swarm_secrets_unmounted filter, picks the ones a rotation left behind
(playbooks/tasks/swarm_secrets_prune.yml).

End-to-end coverage:
    catena-ce ansible/tests/unit/test_catena_admin_service.py
    bench     every install (the converge's deploy), and the bench's recreate
              of a missing panel through the same argv
"""

from __future__ import annotations

import hashlib
import re


# Bind mounts, in the order the compose declared them. `readonly` is the
# whole security story of this list: the panel WRITES its audit hash chain
# and the synced host payload, and reads everything else.
#
# TWO nested rw binds inside the read-only /var/lib/catena, which is one
# directory for one tier of state with the panel's write scope carved out of
# it. ee-payload is where the shell mirrors its embedded host payload at
# startup, for the installer to read back from the host side; admin/ is the
# panel's own state, chiefly its audit hash chain. Everything else under the
# parent belongs to the host lanes and the panel only reads it.
#
# Docker sorts mounts by destination depth, so a nested rw bind shadows the ro
# parent regardless of the order here.
_MOUNTS = (
    ("/var/lib/catena/ee-payload", "/var/lib/catena/ee-payload", False),
    ("/var/lib/catena/admin", "/var/lib/catena/admin", False),
    ("/etc/catena/admin-ssh", "/etc/catena/admin-ssh", True),
    ("/var/backups/catena-export", "/var/backups/catena-export", True),
    ("/var/lib/catena", "/var/lib/catena", True),
    ("/etc/catena/admin-actions.yml", "/etc/catena/admin-actions.yml", True),
    ("/etc/catena/extra-tiles.yml", "/etc/catena/extra-tiles.yml", True),
    # The shell's audit log IS journald (internal/auditlog Emit ->
    # go-systemd journal.Send, which writes to this datagram socket).
    # Without it journal.Enabled() is false inside the container and every
    # audit entry is silently dropped. Send-only: the socket lets the
    # container WRITE journald entries, not read other services' logs.
    ("/run/systemd/journal/socket", "/run/systemd/journal/socket", False),
)

# Where a swarm secret lands inside the container. The env var the shell
# reads is <KEY>_FILE = this + the secret's target name.
SECRET_MOUNT_DIR = "/run/secrets"


def secret_name(base, value):
    """Name a swarm secret after its CONTENT.

    Swarm secrets are immutable, so reusing a fixed name after rotating the
    value leaves the container mounting the OLD bytes forever with nothing
    reporting a problem. Hashing the value into the name makes a rotation a
    different secret, which the drift below then swaps in.

    The hash is truncated to 8 hex chars: this is a cache key over a handful
    of names on one host, not a security boundary -- the secret's bytes are
    protected by swarm, not by the obscurity of its name.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def secret_entries(specs):
    """Resolve [{base, target, value}] into the [{name, target, value}] the
    converge creates and mounts. A blank value is DROPPED rather than stored:
    an empty secret is not a credential, and mounting one would make the
    shell read an empty file instead of falling through to the env, which is
    the documented degraded path (no session key means the native-login
    listener does not start -- a supported state, unlike a listener signing
    cookies with nothing).
    """
    out = []
    for spec in specs or []:
        value = str(spec.get("value") or "")
        if not value.strip():
            continue
        out.append({
            "name": secret_name(spec["base"], value),
            "target": str(spec["target"]),
            "value": value,
        })
    return out


def secrets_unmounted(names, services, bases):
    """The secrets of `names` that secret_name made for one of `bases` and
    that no service of `services` (`docker service inspect` output) mounts:
    what a rotation left behind, for playbooks/tasks/swarm_secrets_prune.yml.
    A name is matched whole, base and 8-hex suffix, so a secret of another
    base sharing a prefix is not touched."""
    if not bases:
        return []
    pattern = re.compile(
        "^(?:" + "|".join(re.escape(str(b)) for b in bases) + ")-[0-9a-f]{8}$")
    mounted = {
        str(entry.get("SecretName") or "")
        for service in services or []
        for entry in _container_spec(service).get("Secrets") or []
        if isinstance(entry, dict)
    }
    return sorted(n for n in names if pattern.match(str(n)) and n not in mounted)


def catena_admin_full_env(spec):
    """The complete environment the service is created with: the plain
    values, plus one <KEY>_FILE per mounted secret.

    The _FILE half is derived here rather than declared by the caller so a
    secret can never be mounted without the shell being told where to read
    it. That failure is invisible: the container starts, the credential is
    sitting in /run/secrets, and the feature it drives is silently off.
    """
    env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    for secret in spec.get("secrets") or []:
        target = str(secret["target"])
        env[f"{target}_FILE"] = f"{SECRET_MOUNT_DIR}/{target}"
    return env


def _mount_arg(source, destination, readonly):
    arg = f"type=bind,source={source},destination={destination}"
    return arg + ",readonly" if readonly else arg


def catena_admin_service_argv(spec):
    """Render the full `docker service create` argv for the panel.

    `spec` carries only per-host values:

        name        service name (also its DNS name on catena-network --
                    oauth2_proxy upstreams to it by that name)
        image       image ref
        networks    overlay networks to attach
        ui_port     host port published for the native-login listener
        direct_port the listener's port inside the container
        group       supplementary gid the container joins
        env         {KEY: value} plain (non-credential) environment
        secrets     [{name, target}] swarm secrets to mount
        hardening   flags from swarm_service_create_args, already rendered

    The image is positional and goes last. No command follows it: the image
    entrypoint is the shell.
    """
    if not isinstance(spec, dict):
        raise ValueError("catena_admin_service_argv: spec must be a dict")
    for key in ("name", "image", "networks", "ui_port", "direct_port"):
        if not spec.get(key) or not str(spec.get(key)).strip():
            raise ValueError(
                f"catena_admin_service_argv: spec.{key} is required")
    if not isinstance(spec["networks"], (list, tuple)):
        raise ValueError("catena_admin_service_argv: spec.networks is a list")

    argv = [
        "docker", "service", "create",
        # Without this the CLI waits for the service to converge -- and with
        # --restart-condition=any below, a task that cannot start is retried
        # forever, so the wait never ends. Under Ansible that is an install
        # parked on this task with no output and no timeout. The convergence
        # wait belongs in the play, where a failure can name the task error.
        "--detach",
        f"--name={spec['name']}",
        *(f"--network={n}" for n in spec["networks"]),
        # A panel that stays down after one bad exit is a panel nobody can
        # use to find out why it exited.
        "--restart-condition=any",
        # mode=host, NOT ingress: the port is firewalled to the tailnet by
        # the public-ports registry, and an ingress publish would put it on
        # the routing mesh where that firewall does not apply.
        f"--publish=mode=host,published={spec['ui_port']},"
        f"target={spec['direct_port']}",
        # The SSH dispatch reaches the host through this name. Swarm stores
        # the literal in the task spec and the daemon resolves it at
        # container create, so host-gateway works here exactly as in compose.
        "--host=host.docker.internal:host-gateway",
    ]

    group = str(spec.get("group") or "").strip()
    if group:
        # The recovery export dir is mode 0750 owned root:<group>. The image
        # runs USER nonroot, which is neither the owner nor in that group, so
        # without this the Recovery tab renders an empty list even when the
        # host wrote a recovery zip.
        argv.append(f"--group={group}")

    for source, destination, readonly in _MOUNTS:
        argv.append("--mount=" + _mount_arg(source, destination, readonly))

    for secret in spec.get("secrets") or []:
        argv.append(
            f"--secret=source={secret['name']},target={secret['target']}")

    env = catena_admin_full_env(spec)
    for key in sorted(env):
        argv.append(f"--env={key}={env[key]}")

    argv += list(spec.get("hardening") or [])
    argv.append(str(spec["image"]))
    return argv


def _task_template(inspect):
    """`docker service inspect` returns a one-element list; accept either
    that or an already-unwrapped dict so a caller can pipe from_json in."""
    if isinstance(inspect, list):
        inspect = inspect[0] if inspect else {}
    if not isinstance(inspect, dict):
        return {}
    return (inspect.get("Spec", {}) or {}).get("TaskTemplate", {}) or {}


def _container_spec(inspect):
    return _task_template(inspect).get("ContainerSpec", {}) or {}


def catena_admin_env_drift(inspect, env):
    """`docker service update` flags that make the live env match `env`.

    Empty list means converged, which is what the role's changed_when keys
    off -- so a converge with nothing to change restarts no task. That
    matters more here than elsewhere: every env update recreates the task,
    and recreating the panel on every converge would make it unavailable
    exactly when a converge is running.

    Keys present on the live service but absent from `env` are REMOVED. The
    panel's environment is fully declared by the converge, and a key left
    behind by an older deploy is how a stale hostname or a revoked token
    keeps being read.
    """
    if not isinstance(env, dict):
        raise ValueError("catena_admin_env_drift: env must be a dict")
    live = {}
    for entry in _container_spec(inspect).get("Env") or []:
        key, _, value = str(entry).partition("=")
        live[key] = value

    args = []
    for key in sorted(env):
        want = str(env[key])
        if live.get(key) != want:
            args += ["--env-add", f"{key}={want}"]
    for key in sorted(set(live) - set(env)):
        args += ["--env-rm", key]
    return args


def catena_admin_secret_drift(inspect, secrets):
    """`docker service update` flags that make the mounted secrets match.

    Secrets are compared by NAME, and the name carries the value's hash, so
    a rotation shows up here as a remove plus an add. Removing by name is
    what `--secret-rm` takes.
    """
    live = {}
    for entry in _container_spec(inspect).get("Secrets") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("SecretName") or "")
        target = str((entry.get("File") or {}).get("Name") or "")
        if name:
            live[name] = target

    want = {str(s["name"]): str(s["target"]) for s in (secrets or [])}

    args = []
    for name in sorted(set(live) - set(want)):
        args += ["--secret-rm", name]
    for name in sorted(set(want) - set(live)):
        args += ["--secret-add", f"source={name},target={want[name]}"]
    return args


def catena_admin_network_drift(inspect, network_ids):
    """`docker service update` flags that make the attached networks match.

    `network_ids` maps each network the service joins to its id, as `docker
    network inspect` reports it: the live spec names a network by id, while
    the converge declares it by name. A blank id raises rather than reading
    as absent, because absent means --network-rm, and removing catena-network
    takes the panel off its gate.
    """
    if not isinstance(network_ids, dict):
        raise ValueError("catena_admin_network_drift: network_ids must be a dict")
    want = {}
    for name, net_id in network_ids.items():
        if not str(net_id or "").strip():
            raise ValueError(
                f"catena_admin_network_drift: no id for network {name}")
        want[str(net_id).strip()] = str(name)
    live = {
        str(entry.get("Target") or "")
        for entry in _task_template(inspect).get("Networks") or []
        if isinstance(entry, dict)
    }

    args = []
    for target in sorted(live - set(want)):
        args += ["--network-rm", target]
    for net_id in sorted(set(want) - live):
        args += ["--network-add", want[net_id]]
    return args


class FilterModule:
    def filters(self):
        return {
            "swarm_secret_entries": secret_entries,
            "swarm_secrets_unmounted": secrets_unmounted,
            "catena_admin_full_env": catena_admin_full_env,
            "catena_admin_service_argv": catena_admin_service_argv,
            "catena_admin_env_drift": catena_admin_env_drift,
            "catena_admin_secret_drift": catena_admin_secret_drift,
            "catena_admin_network_drift": catena_admin_network_drift,
        }
