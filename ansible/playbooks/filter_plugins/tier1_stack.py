"""Ansible filters: ONE spec per directly-created swarm service, rendered as
the compose ORACLE.

    tier1_stack_render(services) -> a compose mapping `docker stack config`
                                    validates
    tier1_spec_upsert(list, spec) -> the accumulating per-converge roster

WHAT APPLIES IS NOT HERE. The services are created and reconciled by the
catena-admin host engine (payload/engines/tier1), dispatched per role from
reconcile/roles/tier1_stack/tasks/reconcile_one.yml. The argv renderer (`docker
service create` argv) lives once, in Go, on the host -- not duplicated here
as a second Ansible renderer. Two renderers of the same spec in two
languages would agree only on the cases somebody wrote a fixture for, and
an Ansible-only renderer is reachable only from a converge, so a control
plane that drifted at 3am would wait for a laptop.

WHAT THIS RENDER IS FOR

  It is the oracle: rendered and handed to `docker stack config` on every
  converge, which type-checks the whole plane against the docker actually
  installed -- not against this file's memory of the compose schema -- and is
  what `--check-swarm` walks. It is never applied.

  `docker stack deploy` was measured as the apply path and lost. It ALWAYS
  names services `<stack>_<key>`; there is no flag to suppress the prefix, and
  deploying over an existing standalone service does not adopt it -- it creates
  a second service beside it. Renaming catena-traefik / catena-postgres /
  catena-portainer reaches 13 DNS sites (Keycloak's JDBC URL, cloudflared's
  ingress target, the Gatus probes, oauth2-proxy's upstream,
  PORTAINER_API_BASE), three anchored matchers in catena-admin's recovery
  engines, and 107 bench assertions. The anchored ones -- pgreplay.go's
  pauseExcludeRe, quiesce.go's isControlPlane, restore.go's isRestoreExempt --
  would stop matching SILENTLY. Not worth the declarative reset and --prune it
  would have bought.

The admin panel is the tier-1 service that is NOT rendered here, and the reason
is the schema rather than a preference: it needs a supplementary group, which
the compose spec cannot express (see the group_add trap below). cloudflared is
absent for an unrelated reason -- its converge is its own Go engine.

SWARM COMPOSE TRAPS, EVERY ONE VERIFIED AGAINST `docker stack config`

  - `host_ip` in a port mapping is REJECTED outright: "services.x.ports.0
    Additional property host_ip is not allowed". A swarm PortConfig has no
    host-IP field, so a loopback bind cannot be expressed. Ports are
    emitted in LONG syntax with mode: host and no host_ip; the firewall,
    not the bind address, is what scopes them (see helpers/public_ports.py).
  - `group_add` is REJECTED the same way -- "services.x Additional property
    group_add is not allowed", under both the versioned and the version-less
    loader. A supplementary GID is expressible in `docker service create
    --group` and NOWHERE in the compose schema, which is why the admin panel
    is the one tier-1 service that keeps its own create argv: it joins the
    group that owns the 0750 recovery export dir, and without that its
    Recovery tab renders empty against a host that did write a zip.
  - `network_mode: host` is SILENTLY IGNORED by stack deploy. Host
    networking is expressed as membership in an EXTERNAL network literally
    named `host`, which this renderer emits when a spec asks for it.
  - top-level `restart:` is SILENTLY IGNORED by stack deploy. The
    equivalent is deploy.restart_policy, and `delay` is not optional: a
    service that crashes against a not-yet-ready dependency hot-loops
    without it.
  - `depends_on` is SILENTLY IGNORED too. Ordering is re-encoded as
    crash-and-retry via restart_policy. Emitting depends_on would read as
    ordering to every future maintainer while doing nothing at all, so
    this renderer refuses to emit it and the unit test pins that.

No `version:` key is emitted. The compose loader stamps its own (3.13 on
the CLI this is verified against) and treats a declared one as obsolete.

SECRETS. Swarm secrets are immutable, so a rotated value must arrive
under a NEW name or the container mounts the old bytes forever. Names are
content-hashed upstream (catena_admin_service.secret_name) and referenced
here as `external: true` -- the stack references secrets, it does not
create them, because creation is what the rotation path owns.

End-to-end coverage:
    catena-ce ansible/tests/unit/test_tier1_stack_render.py
"""

from __future__ import annotations


# The restart delay. Without it a service whose dependency is not up yet
# burns a CPU restarting; ordering in swarm IS crash-and-retry, so the
# delay is the ordering primitive, not a nicety.
RESTART_DELAY = "5s"

# The network name that means host networking. Not a network catena creates:
# every docker daemon ships it, so it is referenced as external like the rest.
HOST_NETWORK = "host"

_REQUIRED = ("name", "image")

# Keys a caller might reach for that swarm cannot honour. Each maps to the
# reason, because the failure mode for all of them is a service that comes up
# looking right and behaving wrong.
_REFUSED = {
    "depends_on": (
        "depends_on is SILENTLY IGNORED by `docker stack deploy` -- it would "
        "read as ordering while doing nothing. Ordering is "
        "deploy.restart_policy (crash-and-retry) plus a readiness probe"
    ),
    "restart": (
        "top-level `restart` is silently ignored by swarm; use "
        "deploy.restart_policy (this renderer emits it for you)"
    ),
    "network_mode": (
        "network_mode is silently ignored by `docker stack deploy`. Host "
        f"networking is membership in the external network {HOST_NETWORK!r} "
        "-- put it in `networks` instead"
    ),
    "group": (
        "a supplementary group is not expressible in a stack file -- "
        "`docker stack config` rejects group_add outright, and the compose "
        "schema has no other spelling. A service that needs one keeps its own "
        "`docker service create` argv (see catena_admin_service.py)"
    ),
    "group_add": (
        "group_add is REJECTED by `docker stack config`; see the `group` "
        "refusal above"
    ),
}


def _fail(msg):
    raise ValueError(f"tier1_stack_render: {msg}")


def swarm_mount_to_compose(mount):
    """`docker service create --mount` value -> a compose `volumes:` entry.

    The roles that predate this renderer each carry their mounts as the
    comma-separated --mount form, which is the form `docker service update`
    also takes. Translating here rather than rewriting five defaults files
    keeps ONE spelling of each mount: the spec the engine applies from and the
    file this renders carry the same value, so they cannot drift.

    Both spellings of the destination are accepted -- `destination=` (traefik,
    postgres, portainer) and `target=` (coturn) -- because docker accepts both
    and the roles use both.
    """
    fields = {}
    for part in str(mount).split(","):
        key, sep, value = part.strip().partition("=")
        # `readonly` is a bare flag in the roles, `readonly=true` is legal too.
        fields[key.strip()] = value.strip() if sep else "true"

    source = fields.get("source") or fields.get("src") or ""
    dest = fields.get("destination") or fields.get("target") or ""
    if not source or not dest:
        _fail(f"mount {mount!r} has no source/destination pair")

    readonly = str(fields.get("readonly", "")).lower() in ("true", "1")
    return f"{source}:{dest}:ro" if readonly else f"{source}:{dest}"


def _deploy_block(spec):
    """deploy: -- everything swarm reads that plain compose does not."""
    deploy = {
        "replicas": int(spec.get("replicas", 1)),
        # condition: any, NOT on-failure. A control-plane service that
        # stays down after one clean exit is a service nobody can use to
        # find out why it exited.
        "restart_policy": {"condition": "any", "delay": RESTART_DELAY},
    }

    mode = str(spec.get("mode") or "").strip()
    if mode == "global":
        # A global service is one task per node and MUST NOT carry
        # replicas; swarm rejects the combination.
        deploy["mode"] = "global"
        deploy.pop("replicas")

    resources = {}
    if "limit_memory" in spec:
        resources["limits"] = {"memory": str(spec["limit_memory"])}
    if "reserve_memory" in spec:
        resources["reservations"] = {"memory": str(spec["reserve_memory"])}
    if resources:
        deploy["resources"] = resources

    update = {}
    if "update_failure_action" in spec:
        action = str(spec["update_failure_action"])
        if action == "rollback":
            # The managed-update engine is the rollback authority: it
            # decides on a whole-host health diff and keeps a persistent
            # quarantine. `--rollback` reverts to the PREVIOUS spec, which
            # after an auto-rollback is the failed one, so swarm rolling
            # back underneath the engine either re-applies the bad image or
            # records a version that never shipped as bumped.
            _fail(
                f"{spec['name']}: update_failure_action must be 'pause', not "
                "'rollback' -- catena-admin payload/engines/stackupdate is "
                "the rollback authority and swarm must not pre-empt it"
            )
        update["failure_action"] = action
    for key in ("update_monitor", "update_order"):
        if key in spec:
            update[key.removeprefix("update_")] = str(spec[key])
    if "update_max_failure_ratio" in spec:
        update["max_failure_ratio"] = float(spec["update_max_failure_ratio"])
    if update:
        deploy["update_config"] = update

    constraints = [str(c) for c in spec.get("constraints") or []]
    if constraints:
        deploy["placement"] = {"constraints": constraints}

    return deploy


def _healthcheck(spec):
    if "health_cmd" not in spec:
        return None
    hc = {"test": ["CMD-SHELL", str(spec["health_cmd"])]}
    if "health_interval" in spec:
        hc["interval"] = str(spec["health_interval"])
    if "health_retries" in spec:
        hc["retries"] = int(spec["health_retries"])
    if "health_start_period" in spec:
        hc["start_period"] = str(spec["health_start_period"])
    return hc


def _ports(spec):
    """Long-syntax ports only.

    Short `"8080:80"` becomes an INGRESS publish on the routing mesh, which
    is not what any of these services want, and the long form is the only
    one that can say mode: host. host_ip is unrepresentable and rejected.
    """
    out = []
    for port in spec.get("ports") or []:
        if not isinstance(port, dict):
            _fail(f"{spec['name']}: ports must be mappings, got {port!r}")
        if "host_ip" in port:
            _fail(
                f"{spec['name']}: host_ip is not expressible in a swarm port "
                "-- `docker stack deploy` rejects the file outright. Scope the "
                "port with the public-ports registry instead"
            )
        entry = {
            "target": int(port["target"]),
            "published": int(port["published"]),
            "protocol": str(port.get("protocol", "tcp")),
            "mode": str(port.get("mode", "host")),
        }
        out.append(entry)
    return out


def _service(spec):
    if not isinstance(spec, dict):
        _fail(f"each service must be a mapping, got {spec!r}")
    for key in _REQUIRED:
        if not str(spec.get(key) or "").strip():
            _fail(f"service spec is missing required key {key!r}")
    for key, reason in _REFUSED.items():
        if key in spec:
            _fail(f"{spec['name']}: {reason}")

    out = {"image": str(spec["image"]), "deploy": _deploy_block(spec)}

    if "stop_grace_period" in spec:
        out["stop_grace_period"] = str(spec["stop_grace_period"])

    networks = [str(n) for n in spec.get("networks") or []]
    if networks:
        out["networks"] = networks

    env = spec.get("environment") or {}
    if env:
        out["environment"] = {str(k): str(v) for k, v in sorted(env.items())}

    # Two spellings, both used on this host. A bare string mounts at
    # /run/secrets/<name> (postgres, portainer read the secret by its own
    # name). A {name, target} pair is what content-hashed names need: the
    # SOURCE carries the value's hash and changes on rotation, while the
    # TARGET is the stable filename the container reads.
    secrets = []
    for secret in spec.get("secrets") or []:
        if isinstance(secret, dict):
            source = str(secret.get("name") or secret.get("source") or "")
            target = str(secret.get("target") or "")
            if not source or not target:
                _fail(f"{spec['name']}: secret {secret!r} needs name + target")
            secrets.append({"source": source, "target": target})
        else:
            secrets.append(str(secret))
    if secrets:
        out["secrets"] = secrets

    volumes = [str(v) for v in spec.get("volumes") or []]
    if volumes:
        out["volumes"] = volumes

    ports = _ports(spec)
    if ports:
        out["ports"] = ports

    hosts = [str(h) for h in spec.get("extra_hosts") or []]
    if hosts:
        out["extra_hosts"] = hosts

    labels = spec.get("container_labels") or {}
    if labels:
        # CONTAINER labels, not service labels: gatus-sync reads `docker ps`,
        # which reports labels on the task container. `deploy.labels` would
        # put them on the service object where nothing looks for them.
        out["labels"] = {str(k): str(v) for k, v in sorted(labels.items())}

    hc = _healthcheck(spec)
    if hc:
        out["healthcheck"] = hc

    if "command" in spec:
        out["command"] = list(spec["command"])
    if "user" in spec:
        out["user"] = str(spec["user"])

    return out


def tier1_spec_upsert(existing, spec):
    """Add `spec` to an accumulating roster, replacing any same-named entry
    already in it. Used for both rosters -- the control plane and the app
    companions -- because the operation is the same either way.

    Each role appends its own spec as it deploys, which is what keeps the
    rendered file and the spec the engine applies reading the SAME variable.
    Replace rather
    than append because a role can legitimately run twice in one converge:
    site.yml re-runs coturn in post_tasks, because the post-restore hooks
    bring the TURN consumer up after reconcile/roles/coturn already probed for one and
    correctly found none. Appending would hand the renderer a duplicate name
    and fail the converge on the recovery path specifically.
    """
    name = str((spec or {}).get("name") or "").strip()
    if not name:
        _fail("tier1_spec_upsert: spec has no name")
    return [s for s in (existing or [])
            if str(s.get("name") or "") != name] + [spec]


def tier1_stack_render(services):
    """Render the whole tier-1 control plane into one compose mapping."""
    if not isinstance(services, list) or not services:
        _fail("services must be a non-empty list of service specs")

    rendered = {}
    for spec in services:
        name = str(spec.get("name") or "").strip() if isinstance(spec, dict) else ""
        if name in rendered:
            _fail(f"duplicate service name {name!r}")
        rendered[name] = _service(spec)

    # Every network, volume and secret the services reference has to be
    # declared at the top level or the file is rejected. All three are
    # EXTERNAL: the overlay is created by the infrastructure role, volumes
    # carry data that predates this render, and secrets are content-hashed
    # by the rotation path. A stack that created them would take ownership
    # of state it cannot safely recreate.
    networks, volumes, secrets = set(), set(), set()
    for spec in services:
        networks.update(str(n) for n in spec.get("networks") or [])
        for secret in spec.get("secrets") or []:
            if isinstance(secret, dict):
                secrets.add(str(secret.get("name") or secret.get("source")))
            else:
                secrets.add(str(secret))
        for vol in spec.get("volumes") or []:
            source = str(vol).split(":", 1)[0]
            # A bind mount starts with / and is not a named volume.
            if source and not source.startswith("/"):
                volumes.add(source)

    # No `version:`. The loader stamps its own and treats a declared one as
    # obsolete; pinning a number here only invites a deprecation warning.
    out = {"services": rendered}
    if networks:
        out["networks"] = {n: {"external": True} for n in sorted(networks)}
    if volumes:
        out["volumes"] = {v: {"external": True} for v in sorted(volumes)}
    if secrets:
        out["secrets"] = {s: {"external": True} for s in sorted(secrets)}
    return out


class FilterModule:
    def filters(self):
        return {
            "tier1_stack_render": tier1_stack_render,
            "tier1_spec_upsert": tier1_spec_upsert,
            "swarm_mount_to_compose": swarm_mount_to_compose,
        }
