# oauth2_proxy

Run one oauth2-proxy container per gated upstream, in reverse-proxy mode.
Traefik routes the whole hostname to the proxy; the proxy authenticates
against Keycloak and forwards to its own `--upstream` backend. Each
instance carries its own `--allowed-group` set, so the ACL is per app even
though the SSO session is shared.

## Responsibilities

- Render the Keycloak OIDC client + secret pair (idempotent: reads
  from the realm if already present, mints if missing).
- Render one compose service per entry in `oauth2_proxy_apps`, named
  `oauth2-proxy-<slug>` (never the bare slug -- the backend usually owns
  that alias, and a collision makes Docker DNS round-robin the proxy into
  itself).
- Render per-app Traefik route files (`<slug>-oauth2-proxy.yml`) at
  priority 100, pointing the host at the proxy rather than the app.
- Deploy the whole set as one swarm stack.

## Inputs

- `oauth2_proxy_cookie_secret` / `oauth2_proxy_client_secret` -- read from
  the on-box store, passed via the env block so they never reach the
  command line.
- `oauth2_proxy_apps` -- list of {slug, host_var, upstream_alias,
  upstream_port, allowed_groups} per gated infra app. Client apps are not
  listed here: dashboard-sync provisions an instance per app from its
  `vps.auth.*` labels.

## Idempotency

- Client secret minting is gated on existence in Keycloak.
- Traefik route files are rendered atomically per app.
- The swarm stack deploy reports a change only when the rendered stack
  file differs.

## Related

- Callers: `playbooks/converge.yml` and `playbooks/reconcile.yml` (after
  `keycloak`).
- Operator-facing: `ops/internal_docs/tools/keycloak-and-oauth2-proxy-gotchas.md`.
