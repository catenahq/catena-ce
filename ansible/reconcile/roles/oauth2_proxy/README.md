# oauth2_proxy

Run one oauth2-proxy container per gated admin tool, in reverse-proxy mode.
Traefik routes the whole hostname to the proxy; the proxy authenticates
against Keycloak and forwards to its own `--upstream` backend. Each
instance carries its own `--allowed-group` set and its own session cookie:
named after its slug, signed with its own secret, and kept on its host (no
cookie domain), so no client app or other admin tool receives or can replay
it. Signing in to one admin tool signs in to that tool alone; the next sends
the browser through Keycloak once, silently while the Keycloak session lasts.

## Responsibilities

- Render the Keycloak OIDC client every instance shares, with the client
  secret the on-box store holds, into the realm import.
- Render one compose service per entry in `oauth2_proxy_apps`, named
  `oauth2-proxy-<slug>` (never the bare slug -- the backend usually owns
  that alias, and a collision makes Docker DNS round-robin the proxy into
  itself).
- Render per-app Traefik route files (`<slug>-oauth2-proxy.yml`) at
  priority 100, pointing the host at the proxy rather than the app.
- Deploy the whole set as one swarm stack. Each instance reads its
  credentials (client secret, cookie secret, the password it sends its
  upstream) from a config file mounted as a swarm secret
  (`templates/oauth2-proxy.cfg.j2`, `--config`), so `docker service inspect`
  shows none of them.

## Inputs

- `oauth2_proxy_client_secret` and each instance's cookie secret -- read
  from the on-box store into the instance's swarm secret.
- `oauth2_proxy_cookie_name` / `oauth2_proxy_cookie_secret` -- the session
  cookie the client apps' gates share across the domain; this role prefixes
  its instances' cookie names with the name.
- `oauth2_proxy_apps` -- list of {slug, host_var, upstream_alias,
  upstream_port, allowed_groups, cookie_secret_var} per gated infra app,
  `cookie_secret_var` naming the store secret the instance signs its cookie
  with, plus two optional fields: `basic_auth_password_var`, naming the
  store secret the instance
  sends its upstream as a Basic password (the panel's
  `catena_admin_gate_secret`, without which the panel serves every request
  as nobody), and `networks`, the overlays it joins besides `catena-network`
  (the private networks of Healthchecks and Beszel's hub). Client apps are
  not listed here:
  dashboard-sync provisions an instance per app from its `vps.auth.*` labels.

## Idempotency

- The realm re-import runs only when the rendered client changed.
- Each swarm secret is named after its content, so a rotated credential is a
  new secret and redeploys the instance that reads it.
- Traefik route files are rendered atomically per app.
- The swarm stack deploy reports a change only when the rendered stack
  file differs.

## Related

- Callers: `playbooks/converge.yml` and `playbooks/reconcile.yml` (after
  `keycloak`).
- Operator-facing: `ops/internal_docs/tools/keycloak-and-oauth2-proxy-gotchas.md`.
