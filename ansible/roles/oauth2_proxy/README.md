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
- Portainer redeploy fires only when the rendered config differs
  from the running.

## Related

- Caller: `playbooks/site.yml` (after `keycloak`).
- Operator-facing: `ops/internal_docs/tools/keycloak-and-oauth2-proxy-gotchas.md`.

## Planned (deferred): public-with-gated-path Traefik shape

Easy!Appointments configures OIDC through config-file edits rather than
env vars, so it sits outside the label-driven wiring flow (see
`ops/internal_docs/adr/0007-easy-appointments-as-the-scheduler.md`).
When its template lands, this role will need a sibling rendering
pattern under a new `templates/` dir:

- `templates/app-public-with-gated-path.yml.j2` -- two Traefik routers
  on the same host, priority-100 anonymous straight to the app +
  priority-200 through the app's oauth2-proxy for `gated_path_prefix`
  (e.g. `/index.php/backend`).

The seeding flow that picks which template to render per-app reads
the catalog's `sso_mode`. The new mode value is `pre-wired-split`
(currently only Easy!Appointments would use it). When a second
consumer surfaces, document the pattern up here in the role README.

Why this is recorded but not built: Easy!Appointments is on the
backlog (no compose template yet). Building the routing pattern
without a consumer means an untested code path.
