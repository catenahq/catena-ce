# traefik

The catena-owned reverse proxy: deploys and owns the `catena-traefik` swarm
service on the `catena-network` overlay, the single origin every `*.<zone>`
wildcard ingress rule (cloudflared) points at.

## What it manages

- The `catena-network` attachable overlay, created here. Every catena app
  stack joins it through the Portainer stack seam, so that one network is all
  Traefik needs to route to.
- The `catena-traefik` swarm service. Its spec -- image, mounts, hardening,
  placement, health probe -- comes from the tier-1 host engine
  (`catena-tier1 spec catena-traefik`) rather than from this role, so the
  desired control plane travels with the catena-admin image.
- The config tree at `/etc/catena/traefik`: `traefik.yml` (static config,
  rendered here) and `dynamic/` (file-provider routes written by three
  producers: `reconcile/roles/oauth2_proxy`, dashboard-sync, and the
  portainer_stack seam). This role guarantees the dynamic dir exists and that
  Traefik watches it; route CONTENT belongs to those producers.
- Two middlewares on the `web` entrypoint, rendered into `dynamic/`:
  `cf-forwarded-proto-https` (cloudflared sends `Cf-Visitor` rather than
  `X-Forwarded-Proto`, and Keycloak reads the latter) and
  `catena-secure-headers`.
- `forwardedHeaders.trustedIPs` scoped to RFC1918 + loopback, which governs
  whose `X-Forwarded-*` Traefik honours among the private sources that can
  reach it.
- Removal of a plain container holding the name, when no swarm service exists
  yet. Gated on the service being absent, so it can never tear down a swarm
  task container.
- A force-roll of the service when `traefik.yml` changed. The static config is
  a bind mount, but Traefik reads it once at startup -- only the file provider
  hot-reloads. Skipped when the engine's own reconcile already rolled the
  task.

## No host ports, no TLS

The service publishes nothing. cloudflared reaches the proxy at
`http://catena-traefik:80` over the overlay, and that is the only path in.
The invariant is structural: neither renderer emits a publish, so there is no
port binding for a drift check to find, and
[tasks/validate.yml](tasks/validate.yml) asserts `EndpointSpec.Ports` is
empty.

The static config declares no `websecure` entrypoint and no
`certificatesResolvers`, so the host holds no ACME storage and the spec mounts
no `acme.json`. TLS terminates at Cloudflare. Adding a resolver means adding
its storage seeding at the same time: the file must exist as a FILE before the
container launches, because a Docker bind mount auto-creates a missing source
as a DIRECTORY, which Traefik cannot open.

## Boundaries

- `catena-network-nudge` belongs to `bootstrap/roles/docker`, which owns both
  ends of the overlay race it heals. `catena-traefik` is a swarm service, so
  the task manager re-dispatches it and the nudge skips it by label; what the
  nudge protects is the Portainer compose containers.
- Validation (`tasks_from: validate.yml`) asserts the service is at N/N
  replicas, publishes no ports, carries RFC1918 in `forwardedHeaders`, and
  that `catena-network` is an overlay.

## Key defaults

The Traefik image is not here. It lives in the host engine's built-in catalog,
where Renovate and Trivy follow it.

| Var | Default | Meaning |
| --- | --- | --- |
| `traefik_container_name` | `catena-traefik` | cloudflared ingress target |
| `catena_network_name` | `catena-network` | shared attachable overlay |
| `traefik_config_dir` | `/etc/catena/traefik` | static + dynamic config root |
| `traefik_dynamic_dir` | `<config_dir>/dynamic` | file-provider directory |
| `traefik_trusted_ips` | RFC1918 + loopback | forwardedHeaders trust scope |
