# infrastructure

Deploys everything that's NOT the auth pair (Keycloak +
oauth2_proxy), the data plane (Dokploy + storage + backup), or
the operator panel (`roles/catena-admin`):

- **Cloudflare Tunnel** -- the cloudflared swarm service that gives
  every public app a hostname under the operator's CF zone without
  any host port bindings.
- **Gatus** -- endpoint monitoring (internal alias + public 302-as-up
  per app; auto-generated config via `gatus-sync.py`). Surfaced to
  the operator inside catena-admin's System tab.
- **Healthchecks** -- self-hosted dead-man-switch service. Backup
  timer, auto-update timer, gatus, etc. ping it; missed pings
  alert via ntfy. Surfaced to the operator inside catena-admin's
  System tab.
- **Recovery secret export** -- cron-emit of the GPG-symmetric
  secrets bundle (driven by the
  `Generate recovery archive (encrypted)` entry in catena-admin's
  Actions tab) so the operator can recover access even after vault
  key loss.
- **Sync timers** -- systemd timers around `dashboard-sync.py` and
  `gatus-sync.py`. `catena-version-check.py` (auto-detecting Versions
  report) runs as gatus-sync's ExecStartPre producer, not its own timer.

## Auxiliary task files

- `_seed_bind_mount_file.yml` -- helper for templates that ship a
  config file via bind-mount.
- `_github_provider_assert.yml` -- preflight assertion that every
  GitHub App alias declared in `expected_github_providers` is
  registered in this Dokploy instance. Manual one-time browser
  flow per alias; this task fails with the operator walkthrough
  if any are missing. Used by webapps deployed via Dokploy's
  native git-source compose flow (catena's own + per-client).
- `snapshot_dokploy_state.yml` -- capture compose state for the
  pre-rotate snapshot used by the auto-update tier 2 flow.

Catena's own webapps (website + portal) are no longer deployed
by this role. They ship a `dokploy.compose.yml` per app and the
operator creates the Dokploy compose manually from the UI; see
`internal_docs/operator/deploy-webapp-from-github.md`. The
Keycloak realm client for the portal still lives in Ansible and
is provisioned by `roles/keycloak/tasks/_portal_realm.yml`.

## Inputs

- `vault_cloudflare_api_token`, `cloudflare_*_id`, `cloudflare_zone`
- `vault_healthchecks_*` (api keys, ntfy URL)
- `infrastructure_apps_enabled` -- toggle list per first-class app.

## Idempotency

- Every Dokploy compose deploy goes through the API and is gated
  on a shape comparison; idempotent across re-runs.
- Sync timers are templated with stable content.
