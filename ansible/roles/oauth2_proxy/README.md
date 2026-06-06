# oauth2_proxy

Configure the two oauth2-proxy instances that gate the stack:

- **staff** -- protects every client-facing app (Outline,
  Easy!Appointments, Rocket.Chat...) via the `staff` group.
- **admin** -- protects operator-only surfaces (admin.<zone>,
  Dokploy UI, Healthchecks UI, OliveTin) via the `operators` /
  `client-admin` groups.

## Responsibilities

- Render the Keycloak OIDC client + secret pair (idempotent: reads
  from the realm if already present, mints if missing).
- Render Traefik forward-auth middleware definitions per instance.
- Render per-app route files (one Traefik dynamic config per
  protected app) with the correct group filter and unprotected-path
  allowlist.
- Deploy both compose projects via the Dokploy API.

## Inputs

- `vault_oauth2_proxy_cookie_secret_staff` /
  `vault_oauth2_proxy_cookie_secret_admin` -- 32-byte secrets,
  rotation via `runbooks/rotate-oauth2-proxy-cookie.md`.
- `oauth2_proxy_protected_apps` -- list of {name, host, group,
  unauth_paths} per app.

## Idempotency

- Client secret minting is gated on existence in Keycloak.
- Traefik route files are rendered atomically per app.
- Dokploy redeploy fires only when the rendered config differs
  from the running.

## Related

- Caller: `playbooks/site.yml` (after `keycloak`).
- Operator-facing: `internal_docs/operator/keycloak-and-oauth2-proxy-gotchas.md`.

## Planned (deferred): public-with-gated-path Traefik shape

When the Easy!Appointments template lands (see
`internal_docs/operator/external-scheduler-comparison.md` section 7),
this role will need a sibling rendering pattern under a new
`templates/` dir:

- `templates/app-public-with-gated-path.yml.j2` -- two Traefik routers
  on the same host, priority-100 anonymous + priority-200
  forward-auth-gated on `gated_path_prefix` (e.g.
  `/index.php/backend`).

The seeding flow that picks which template to render per-app reads
the catalog's `sso_mode`. The new mode value is `pre-wired-split`
(currently only Easy!Appointments would use it). When a second
consumer surfaces, document the pattern up here in the role README.

Why this is recorded but not built: Easy!Appointments is on the
backlog (no compose template yet). Building the routing pattern
without a consumer means an untested code path.
