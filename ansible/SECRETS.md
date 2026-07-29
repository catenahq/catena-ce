# Catena secret + config classification (0b reference)

Source of truth for **where every secret and config value comes from, who
holds it, and where it must end up** under the client-owned-config model
(0b). Derived from `helpers/onbox_config.py` (`INTERNAL_SECRETS` +
`USER_HELD_SECRETS` minted on-box; `EXTERNAL_SECRETS` client-supplied;
`ROLE_MINTED_SECRETS` minted by the service and captured by its role),
`seed.py` (`INSTALL_EXTERNAL_KEYS` -- the only creds prompted at install,
written to the transient `--secrets-out` adopt file and nowhere else),
`inventory/example/.env.example`, and `roles/backup/defaults/main.yml`
(`backup_paths`). **Nothing secret is persisted on the controller** --
`catena install` writes no secret file into the inventory.

## North star

A client rebuilds their VPS from **only** what they keep in their own
password manager:

- the restic repo URL (`BACKUP_RESTIC_REPO`),
- the S3 access + secret key for that bucket,
- the restic encryption password.

Everything else -- every catena-internal secret, all non-secret config --
lives **on-box** under a path that rides the restic backup, so a restore
brings it back with the data. The operator holds nothing.

## Four categories

Every `vault_*` name referenced anywhere under `ansible/` belongs to exactly
one of them. "Belongs to none" is not a state -- it is what let the Portainer
API key sit in a comment for a release instead of in a registry.

### 1. Client-supplied external (settings page / minimal bootstrap input)

Issued by a third party; catena can never generate these. The client enters
them once, they persist on-box, and they ride the backup thereafter. The
DR-critical subset (restic repo + S3 + restic password) is ALSO what the
client keeps in their password manager -- it is the only thing that cannot
ride the backup, because it is what unlocks the backup.

| Key | Purpose | Phase where entered | DR-critical (client-kept) |
| --- | --- | --- | --- |
| `tailscale_oauth_client_id` | Join the client's own tailnet | minimal bootstrap | no |
| `tailscale_oauth_client_secret` | ^ | minimal bootstrap | no |
| `cloudflare_api_token` | Tunnel + DNS | catena-admin Settings (never at install) | no |
| `BACKUP_RESTIC_REPO` (.env) | restic repo URL | settings page | **yes** |
| `backup_s3_access_key` | reach the restic bucket | settings page | **yes** |
| `backup_s3_secret_key` | ^ | settings page | **yes** |
| `backup_restic_password` | decrypt the restic repo | on-box mint, **shown once** | **yes** |
| `admin_password` | first login (Portainer + Keycloak) | on-box mint, **shown once** | no |
| `console_recovery_password` | break-glass login for `ops` at the provider KVM / serial console | on-box mint, **shown once** | **yes** |
| `smtp_password` | outbound mail (opt) | settings page | no |
| `mailserver_relay_password` | smarthost (opt) | settings page | no |
| `mailserver_spamhaus_dqs_key` | RBL (opt) | settings page | no |
| `nextcloud_s3_access_key` | NC primary S3 (opt) | settings page | no |
| `nextcloud_s3_secret_key` | ^ | settings page | no |
| `storage_bulk_username` | CIFS bulk mount (opt; NFS needs neither) | settings page | no |
| `storage_bulk_password` | ^ | settings page | no |

`backup_restic_password`, `admin_password` and
`console_recovery_password` are special: `USER_HELD_SECRETS` in
`onbox_config.py`. They are minted **on-box if absent**
(like the internal secrets) but the installer reads them back and **shows them
once** at the end of `catena install` (`playbooks/show_dr_keyset.yml`) so the
client keeps a copy in their password manager. They are NOT settable through
the settings config-write API (a restic re-key is a deliberate action). On
`catena recover` the client re-enters the saved values; the loader adopts them
into the store BEFORE the restore decrypts the backup (adopt is fill-only, so
the freshly-minted value is only used on a first install). Minting the restic
password on-box is safe precisely because it is surfaced once off-box: without
that copy a lost box is unrecoverable, which is the client's responsibility.

The console password is the same shape one layer down: SSH is key-only, so it
is rejected there and works ONLY at a local console (provider KVM/serial or a
physical keyboard). That is the path back in when the tailnet is gone -- and a
copy that lives only inside the box it unlocks is no copy at all.

### 2. Self-generated ON-BOX (minted at converge, persist on-box, ride backup)

No human ever supplies these and they NEVER touch the client's laptop. The
converge loader (`playbooks/tasks/load_onbox_config.yml` ->
`helpers/onbox_config.py` `ensure_internal_secrets`) mints every missing one
**on the box** (reconcile-not-overwrite) into the on-box config store
(`/etc/catena/config.json`, 0600 root), which persists under a backed-up path,
then set_facts them for the roles. `seed.py` mints NONE of these (that was the
old laptop-minting model; dropped with the 0b true-on-box-minting cutover).

- `catena_postgres_password`
- `keycloak_db_password`
- `oauth2_proxy_cookie_secret`
- `oauth2_proxy_client_secret`
- `dashboard_sync_client_secret`
- `nextcloud_oidc_client_secret`
- `element_oidc_client_secret`
- `mailserver_oidc_client_secret`
- `mailserver_introspect_client_secret`
- `healthchecks_secret_key`
- `healthchecks_superuser_password`
- `healthchecks_ping_key`
- `healthchecks_api_key_readonly`
- `healthchecks_api_key_readwrite`
- `turn_static_auth_secret`
- `nextcloud_talk_signaling_secret`
- `nextcloud_talk_internal_secret`
- `jitsi_prosody_password`
- `jitsi_jicofo_auth_password`
- `jitsi_jicofo_component_secret`
- `jitsi_jvb_auth_password`
- `element_jitsi_jicofo_auth_password`
- `element_jitsi_jicofo_component_secret`
- `element_jitsi_jvb_auth_password`
- `element_jigasi_xmpp_password`
- `beszel_admin_password`
- `beszel_universal_token`

### 3. Service-minted, role-captured (`ROLE_MINTED_SECRETS`)

Minted by the service ITSELF, not by this repo and not by a human. The role
that provisioned the service captures the value and writes it into the same
on-box store, so it rides the backup like every category-2 secret -- but it is
neither generated by `onbox_config.py` nor accepted through the settings API,
because it has exactly one writer and that writer is the role.

| Key | Minted by | Captured by |
| --- | --- | --- |
| `portainer_api_key` | Portainer's own token API, via `helpers/bootstrap_portainer_admin.py` | `roles/portainer` |

The mint runs mid-converge rather than in the loader: the initial admin has to
exist first. The helper prints the key on stdout and the role writes it
straight into the store, so it never touches a file off-box. The role re-mints
with `--overwrite` when the live Portainer rejects the stored key (a `/data`
restore replaces the BoltDB the token lived in), which is why fill-only adopt
is not sufficient here.

### 4. Non-secret config (.env today; rides backup once on-box)

Split by the two-phase install boundary:

**Minimal bootstrap (needed to bring the stack + auth up):** `CLOUDFLARE_ZONE`,
`CLOUDFLARE_ACCOUNT_ID`, the subdomain set (`PORTAINER_SUBDOMAIN`,
`MONITOR_SUBDOMAIN`, `DASH_SUBDOMAIN`, `HEARTBEAT_SUBDOMAIN`,
`AUTH_SUBDOMAIN`), `ADMIN_EMAIL`, `CATENA_DEFAULT_LANGUAGE`, `TAILSCALE_TAGS`,
`OPS_USER`, `COMMON_TIMEZONE`, `COMMON_LOCALE`, `STORAGE_MODE` + mount points.
Plus the two external creds required to bootstrap: Tailscale OAuth (to join the
tailnet) and the Cloudflare token (tunnel + DNS).

**Settings page (post-install):** `BACKUP_RESTIC_REPO`, backup retention +
tier, WORM/cold repo, SMTP host/port/from, `NTFY_*`, `NEXTCLOUD_*` (S3 +
retention), mailserver toggles, docker/apt proxy.

## On-box persistence target

`/etc/catena/` already rides the backup (`backup_paths` includes `/etc`) and
already holds `backup.env` (S3 creds) + `restic.pass` (restic password),
both written reconcile-not-overwrite by `roles/backup`. That is the model
for every category-2 secret and category-4 value: a single on-box config
source-of-truth under `/etc/catena/`, written once, reconciled on converge,
carried in every snapshot. The controller-side inventory is non-secret only
(`.env`, `hosts.yml`, `group_vars/all/main.yml`); nothing writes a secrets
file there.

The swarm-secret path (catena-postgres, portainer admin) is NOT backed up
(`/var/lib/docker/swarm` is excluded); those replay correctly because the
data volume restores raw and the on-box-persisted password re-supplies the
matching credential at converge.

## Resolved decisions

- **At-rest encryption of on-box config.** RESOLVED: 0600 root-only cleartext.
  On a client-owned box the operator is not a recipient, and the box is
  already the trust boundary -- anyone with root has the running secrets
  anyway. Encrypting the store would add a key-management problem without
  moving that boundary, so no encryption tool and no client-held key are in
  play at all.
- **Single config file format.** RESOLVED: JSON at `/etc/catena/config.json`
  (0600 root), with two sections (`secrets` + `config`). Both the
  settings-write API and the converge read/write it atomically
  (temp-file + `os.replace`) via `helpers/onbox_config.py`.
