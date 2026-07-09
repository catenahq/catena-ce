# Catena secret + config classification (0b reference)

Source of truth for **where every secret and config value comes from, who
holds it, and where it must end up** under the client-owned-config model
(0b). Derived from `seed.py` (`VAULT_SKIP_KEYS` + the `_resolve_*` / `_auto_mint_group`
minters), `inventory/example/group_vars/all/vault.yml.example`,
`inventory/example/.env.example`, and `roles/backup/defaults/main.yml`
(`backup_paths`).

## North star

A client rebuilds their VPS from **only** what they keep in their own
password manager:

- the restic repo URL (`BACKUP_RESTIC_REPO`),
- the S3 access + secret key for that bucket,
- the restic encryption password.

Everything else -- every catena-internal secret, all non-secret config --
lives **on-box** under a path that rides the restic backup, so a restore
brings it back with the data. The operator holds nothing.

## Three categories

### 1. Client-supplied external (settings page / minimal bootstrap input)

Issued by a third party; catena can never generate these. The client enters
them once, they persist on-box, and they ride the backup thereafter. The
DR-critical subset (restic repo + S3 + restic password) is ALSO what the
client keeps in their password manager -- it is the only thing that cannot
ride the backup, because it is what unlocks the backup.

| Key | Purpose | Phase where entered | DR-critical (client-kept) |
| --- | --- | --- | --- |
| `vault_tailscale_oauth_client_id` | Join YOUR OWN tailnet | minimal bootstrap | no |
| `vault_tailscale_oauth_client_secret` | ^ | minimal bootstrap | no |
| `vault_cloudflare_api_token` | Tunnel + DNS | minimal bootstrap | no |
| `BACKUP_RESTIC_REPO` (.env) | restic repo URL | settings page | **yes** |
| `vault_backup_s3_access_key` | reach the restic bucket | settings page | **yes** |
| `vault_backup_s3_secret_key` | ^ | settings page | **yes** |
| `vault_backup_restic_password` | decrypt the restic repo | self-gen, then **exported** | **yes** |
| `vault_smtp_password` | outbound mail (opt) | settings page | no |
| `vault_mailserver_relay_password` | smarthost (opt) | settings page | no |
| `vault_mailserver_spamhaus_dqs_key` | RBL (opt) | settings page | no |
| `vault_nextcloud_s3_access_key` | NC primary S3 (opt) | settings page | no |
| `vault_nextcloud_s3_secret_key` | ^ | settings page | no |

`vault_backup_restic_password` is special: catena mints it (48 random bytes)
but the client MUST export + keep it, because it is the key to their own
backup. The settings page exposes it read-once + an "I've saved this" gate.

### 2. Self-generated on-box (mint at converge, persist on-box, ride backup)

No human ever supplies these. Today `seed.py` mints them on the operator/client
laptop and writes them into the laptop-side `vault.sops.yml`. Under 0b they mint
**on the box** at converge time (reconcile-not-overwrite), persist under a
backed-up path, and never touch a laptop vault. The `roles/portainer` API-key
mint (`bootstrap_portainer_admin.py`) is the existing on-box-mint precedent --
except it writes the key BACK to the laptop vault via `sops --set`; 0b inverts
that to write on-box.

- `vault_admin_password` (shared Portainer + Keycloak) -- self-gen, but
  client-visible: surfaced + resettable via the settings page (they log in
  with it).
- `vault_catena_postgres_password`
- `vault_keycloak_db_password`
- `vault_oauth2_proxy_cookie_secret`
- `vault_oauth2_proxy_client_secret`
- `vault_dashboard_sync_client_secret`
- `vault_nextcloud_oidc_client_secret`
- `vault_element_oidc_client_secret`
- `vault_mailserver_oidc_client_secret`
- `vault_mailserver_introspect_client_secret`
- `vault_healthchecks_secret_key`
- `vault_healthchecks_superuser_password`
- `vault_healthchecks_ping_key`
- `vault_healthchecks_api_key_readonly`
- `vault_healthchecks_api_key_readwrite`
- `vault_turn_static_auth_secret`
- `vault_nextcloud_talk_signaling_secret`
- `vault_nextcloud_talk_internal_secret`
- `vault_jitsi_prosody_password`
- `vault_jitsi_jicofo_auth_password`
- `vault_jitsi_jicofo_component_secret`
- `vault_jitsi_jvb_auth_password`
- `vault_element_jitsi_jicofo_auth_password`
- `vault_element_jitsi_jicofo_component_secret`
- `vault_element_jitsi_jvb_auth_password`
- `vault_element_jigasi_xmpp_password`
- `vault_beszel_admin_password`
- `vault_beszel_universal_token`
- `vault_portainer_api_key` (already minted on-box; 0b persists it on-box
  instead of writing back to the laptop vault)

### 3. Non-secret config (.env today; rides backup once on-box)

Split by the two-phase install boundary:

**Minimal bootstrap (needed to bring the stack + auth up):** `CLOUDFLARE_ZONE`,
`CLOUDFLARE_ACCOUNT_ID`, the subdomain set (`PORTAINER_SUBDOMAIN`,
`DOKPLOY_MONITOR_SUBDOMAIN`, `DOKPLOY_DASH_SUBDOMAIN`, `HEARTBEAT_SUBDOMAIN`,
`AUTH_SUBDOMAIN`), `ADMIN_EMAIL`, `CATENA_DEFAULT_LANGUAGE`, `TAILSCALE_TAGS`,
`OPS_USER`, `COMMON_TIMEZONE`, `COMMON_LOCALE`, `STORAGE_MODE` + mount points.
Plus the two external creds required to bootstrap: Tailscale OAuth (to join the
tailnet) and the Cloudflare token (tunnel + DNS).

**Settings page (post-install):** `BACKUP_RESTIC_REPO`, backup retention +
tier, WORM/cold repo, SMTP host/port/from, `NTFY_*`, `NEXTCLOUD_*` (S3 +
retention), `BESZEL_ENABLED`, mailserver toggles, docker/apt proxy.

## On-box persistence target

`/etc/catena/` already rides the backup (`backup_paths` includes `/etc`) and
already holds `backup.env` (S3 creds) + `restic.pass` (restic password),
both written reconcile-not-overwrite by `roles/backup`. That is the model
for every category-2 secret and category-3 value: a single on-box config
source-of-truth under `/etc/catena/`, written once, reconciled on converge,
carried in every snapshot. Prefer ONE referenced file over today's
`.env` + `vault.sops.yml` + `hosts.yml` split (see `project_config_layout`).

The swarm-secret path (catena-postgres, portainer admin) is NOT backed up
(`/var/lib/docker/swarm` is excluded); those replay correctly because the
data volume restores raw and the on-box-persisted password re-supplies the
matching credential at converge.

## Open decisions (resolve in Phase C)

- **At-rest encryption of on-box config.** On a client-owned box the operator
  is not a recipient. Options: SOPS+age with a client-held key (the client
  keeps the age key alongside the restic password), a sealed on-box key, or
  0600 root-only cleartext (the box is already the trust boundary; anyone
  with root has the running secrets anyway). Leaning 0600 root-only for the
  self-gen category, client-held age only if we keep an exportable vault.
- **Single config file format** (YAML under `/etc/catena/config.yml`?) and
  how the settings-write API and the converge both read/write it atomically.
