# cloudflare_tunnel

Find-or-create the Cloudflare Named Tunnel for this VPS, render the
wildcard DNS record (`*.<zone>` -> `<tunnel-id>.cfargotunnel.com`),
enforce the tunnel ingress config (single rule pointing at
`http://dokploy-traefik:80` plus a `404` fallback), and deploy
`cloudflared` as a swarm service on `dokploy-network`.

## Why a separate role (not part of `infrastructure`)

Extracted from `roles/infrastructure` on 2026-05-08 so site.yml can
deploy the public path BEFORE the SSO roles run. The `oauth2_proxy`
role waits for `https://auth.<zone>/.well-known/openid-configuration`
to answer 200 before it deploys its compose; that URL is only reachable
once cloudflared + the wildcard DNS + tunnel ingress are all in place.
The `infrastructure` role still runs AFTER `oauth2_proxy` (the gated
apps it deploys -- Gatus, OliveTin, Homepage, ... -- need oauth2_proxy's
forward-auth chain rendered first), so cloudflared can't ship there.

Pre-refactor, single-pass site.yml on a fresh DR / migrate target
always failed at oauth2_proxy's wait task with CF 530. Post-refactor,
site.yml is idempotent in one pass.

## Site.yml position

```yaml
roles:
  - common
  - tailscale
  - storage
  - docker
  - dokploy           # initializes swarm + creates dokploy-network
  - cloudflare_tunnel # <- this role
  - keycloak
  - oauth2_proxy      # waits for auth.<zone>; cloudflared is up by here
  - infrastructure    # gated apps using oauth2_proxy chain
  - ...
```

## Dependencies

- `dokploy-network` exists (provided by `roles/dokploy`).
- Docker swarm initialized (provided by `roles/dokploy`).
- Vault: `vault_cloudflare_api_token` (Zone:DNS:Edit + Cloudflare
  Tunnel:Edit on the target zone).
- Inventory `.env`: `CLOUDFLARE_ZONE`, `CLOUDFLARE_ACCOUNT_ID`.

## Idempotency

Every API call is find-or-create / PUT-desired-state. The
swarm-service block inspects for existence before `service create`
and uses `service update --env-add` to refresh the tunnel token
in place.

## Companion role

`roles/cloudflare_tunnel_regenerate` is the out-of-band rotation
path -- it deletes the prior tunnel + connectors via the CF API,
then `include_role`s back into this role's find-or-create logic
with `vault_cloudflare_api_token` shadowed by an operator-supplied
token.
