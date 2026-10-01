# docker

Install Docker CE from the official upstream apt repository (matched to
the host's own distro codename) at the pinned engine version, held, with a
pre-staged `daemon.json` and a custom `data-root` placed BEFORE the
package install -- so the daemon starts on first boot already pointed
at the operator-chosen directory (`{{ storage_mount_point }}/docker` by
default), instead of seeding `/var/lib/docker` and forcing an
out-of-band relocation later. Also initializes the single-node Docker
Swarm every catena-owned service runs on; `reconcile/roles/swarm`
configures it.

## Inputs

- `docker_version` / `docker_containerd_version` -- the engine and
  containerd.io a first install gets. catena-admin's `catena-engine-upgrade`
  reads the same two keys to move an installed host; Renovate proposes bumps
  (`renovate.json`).
- `docker_data_root` -- defaults to `{{ storage_mount_point }}/docker`.
- `docker_log_max_size` / `docker_log_max_files` -- daemon.json log
  rotation knobs (json-file driver).
- `docker_live_restore` -- must stay `false`; incompatible with swarm
  mode, which the control plane requires.
- `docker_swarm_advertise_addr` -- defaults to `ansible_host`, the address
  the installer reaches the host at.
- `docker_daemon_control_timeout` -- bound on a `systemctl` call
  against a wedged daemon; defaults to 120.
- `docker_registry_mirror_url` -- optional pull-through mirror; adds
  `registry-mirrors` (and, for `http://`, the matching
  `insecure-registries` entry) to `daemon.json`.

## Side effects

- Adds the Docker CE apt source (pinned to `ansible_facts['distribution_release']`) + GPG key.
- Writes `/etc/docker/daemon.json` BEFORE installing.
- On a host without an engine, installs `docker-ce`, `docker-ce-cli` and
  `containerd.io` at the pinned versions; an installed engine is left where
  it is. Installs `docker-buildx-plugin` and `docker-compose-plugin`.
- Holds `catena_held_engine_packages` (the engine trio), so a
  `dist-upgrade` and unattended-upgrades leave them alone;
  `playbooks/uninstall.yml` releases the hold.
- Detects and recovers a phantom-installed dpkg state (purge + clean
  reinstall) -- seen after a DR restore.
- Initializes a single-node swarm advertising on `docker_swarm_advertise_addr`.
- Sets `DEFAULT_FORWARD_POLICY=ACCEPT` in ufw for container egress.
- Adds the `ops` user to the `docker` group (no sudo for compose).

## Idempotency

- apt module reports "ok" on already-installed.
- daemon.json is templated; only rewritten when content drifts.
- Swarm init is read-then-set, skipped when the node is already a member.

## Related

- Downstream: `reconcile/roles/swarm` (configures the swarm), then `traefik` /
  `postgres` / `portainer` (swarm services need Docker + swarm up first).
