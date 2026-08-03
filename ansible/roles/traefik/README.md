# traefik

The catena-owned reverse proxy: deploys and owns the `catena-traefik`
container on the `catena-network` overlay, the single origin every
`*.<zone>` wildcard ingress rule (cloudflared) points at.

## What it manages

- The `catena-traefik` swarm service, whose spec (image, mounts,
  hardening, placement, health probe) comes from the tier-1 host engine
  via `catena-tier1 spec catena-traefik` rather than from this role. Published on
  the host's :80 (Cloudflare terminates TLS at the edge; no host TLS,
  no Let's Encrypt for the web entrypoint -- LE conflicts with the
  tunnel).
- The config tree at `/etc/catena/traefik`: `traefik.yml` (static
  config), `dynamic/` (file-provider routes written by three
  producers: roles/oauth2_proxy, dashboard-sync, and the
  portainer_stack seam), and `dynamic/acme.json` (0600, seeded and
  self-healed by this role; used by the DNS-01 flows).
- `forwardedHeaders.trustedIPs` scoped to RFC1918 + loopback: only
  private sources (cloudflared on the overlay, Docker bridges) reach
  :80 at all; anything else gets its `X-Forwarded-*` rewritten, which
  keeps Keycloak's Secure-cookie logic correct.
- The catena-network-nudge recovery unit: when a docker.service start
  re-materializes the overlay network, it starts what the race stranded --
  traefik whenever it is not running, plus any other container docker
  meant to keep running but failed to start with "network not found".
  This role owns it because it owns the overlay.
- One-time cutover tasks for hosts born in the pre-Portainer era:
  remove the legacy install.sh-born container, its nudge artifacts,
  and the superseded forwardAuth admin route.

## Boundaries

- Route CONTENT is owned by the producers listed above, never by this
  role: it guarantees the dynamic dir exists and Traefik watches it.
- Validation (`tasks_from: validate.yml`) asserts the container is up,
  the dynamic dir is intact, and the legacy admin route stays gone.

## Key defaults

| Var | Default | Meaning |
| --- | --- | --- |
| `traefik_container_name` | `catena-traefik` | cloudflared ingress target |
| `catena_network_name` | `catena-network` | shared attachable overlay |
| `traefik_config_dir` | `/etc/catena/traefik` | static + dynamic config root |
| `traefik_trusted_ips` | RFC1918 + loopback | forwardedHeaders trust scope |
