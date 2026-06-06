# docker

Install Docker CE from the official upstream apt repository, with
a pre-staged `daemon.json` and a custom `data-root` placed BEFORE
the package install -- so the daemon starts on first boot already
pointed at the operator-chosen directory (`/mnt/data/docker` by
default), instead of seeding `/var/lib/docker` and forcing an
out-of-band relocation later.

## Inputs

- `docker_data_root` -- defaults to `/mnt/data/docker`.
- `docker_version` -- apt pin, e.g. `5:27.5.0-1~deb12.0~bookworm`.
  Matches the controller's known-good version.
- `docker_log_driver` / `docker_log_max_size` -- daemon.json knobs
  for log rotation.

## Side effects

- Adds the Docker CE apt source + GPG key.
- Writes `/etc/docker/daemon.json` BEFORE installing.
- Installs `docker-ce`, `docker-ce-cli`, `containerd.io`,
  `docker-compose-plugin`.
- Adds the `ops` user to the `docker` group (no sudo for compose).

## Idempotency

- apt module reports "ok" on already-installed.
- daemon.json is templated; only rewritten when content drifts.

## Related

- Downstream: `dokploy` (single-node swarm needs Docker up first).
