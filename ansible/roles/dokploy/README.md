# dokploy

Install + harden the Dokploy single-node swarm deployment. Manages
version drift via service updates, removes host port bindings (the
zero-host-ports architecture; ingress flows via Cloudflare Tunnel
+ tailscale only), seeds the dokploy-postgres user/DB used by other
roles (Keycloak), and runs post-install reconciliation.

## Responsibilities

- Run Dokploy's `install.sh` (idempotent -- leaves the swarm in
  place when it's already initialized).
- Remove default host port bindings (3000, 8080, 5432) so nothing
  is exposed on the public IP.
- Wait for the dokploy + dokploy-postgres + dokploy-traefik
  services to converge before returning.
- Patch the `Web Server` admin domain via the Dokploy API
  (deferred to `site.yml` post-task; the role provides the helper
  task file).
- pg_password reconciliation: replay the latest pg_dumpall after a
  raw-volume restore (gated by the post-restore marker).

## Inputs

- `dokploy_version` -- pinned tag, e.g. `v0.29.x`.
- `vault_dokploy_api_key` -- only required after first-boot.
- `dokploy_admin_hostname` -- `admin.<zone>` for the API admin URL.

## Side effects

- Modifies the docker swarm (services, networks, configs).
- Writes the dokploy-postgres password into the catena vault on
  first install.

## Idempotency

- install.sh is itself idempotent (with the `docker swarm leave
  --force` quirk noted in the operator gotchas).
- All Dokploy API calls are gated on shape checks (no-op when the
  desired state is already in place).

## Related

- Operator gotchas: `internal_docs/operator/keycloak-and-oauth2-proxy-gotchas.md`.
