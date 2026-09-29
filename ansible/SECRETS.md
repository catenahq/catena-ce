# Catena secret + config classification

Source of truth for **where every secret and config value comes from, who
holds it, and where it must end up** under the client-owned-config model.
Derived from:

- `helpers/knobs.yml`, the registry: every value a client supplies, with
  its residence, its `.env` default and its panel shape. `EXTERNAL_SECRETS`
  and the non-secret config maps are read from it, and
  `inventory/example/.env.example` is rendered from it.
- `helpers/onbox_config.py` for the three categories no client supplies:
  `INTERNAL_SECRETS` + `USER_HELD_SECRETS` minted on-box, and
  `ROLE_MINTED_SECRETS` minted by the service and captured by its role.
- `seed.py`, which collects no secret: an install.yaml may pin the admin
  password, written to the transient `--secrets-out` adopt file and nowhere
  else, and names no other.
- `reconcile/roles/backup/defaults/main.yml` (`backup_paths`).

**Nothing secret is persisted on the controller** -- `catena-cli install`
writes no secret file into the inventory.

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

### 1. Client-supplied external (catena-admin Settings)

Issued by a third party; catena can never generate these. The client enters
them in catena-admin > Settings once the server runs, they persist on-box,
and they ride the backup thereafter. The
DR-critical subset (restic repo + S3 + restic password) is ALSO what the
client keeps in their password manager -- it is the only thing that cannot
ride the backup, because it is what unlocks the backup.

| Key | Purpose | Phase where entered | DR-critical (client-kept) |
| --- | --- | --- | --- |
| `tailscale_oauth_client_id` | Join the client's own tailnet | settings page | no |
| `tailscale_oauth_client_secret` | ^ | settings page | no |
| `headscale_api_key` | ^, on the self-hosted backend | settings page | no |
| `headscale_preauth_key` | ^, static fallback | settings page | no |
| `cloudflare_api_token` | Tunnel + DNS | settings page | no |
| `BACKUP_RESTIC_REPO` (config) | restic repo URL | settings page | **yes** |
| `backup_s3_access_key` | reach the restic bucket | settings page | **yes** |
| `backup_s3_secret_key` | ^ | settings page | **yes** |
| `backup_restic_password` | decrypt the restic repo | generated in the settings page, **shown once** | **yes** |
| `admin_password` | first login (Portainer + Keycloak + Beszel + this panel) | on-box mint, **shown once** | no |
| `console_recovery_password` | break-glass login for `ops` at the provider KVM / serial console | on-box mint, **shown once** | **yes** |
| `smtp_password` | outbound mail (opt) | settings page | no |
| `mailserver_relay_password` | smarthost (opt) | settings page | no |
| `mailserver_spamhaus_dqs_key` | RBL (opt) | settings page | no |
| `storage_bulk_username` | CIFS bulk mount (opt; NFS needs neither) | settings page | no |
| `storage_bulk_password` | ^ | settings page | no |

`backup_restic_password`, `admin_password` and
`console_recovery_password` are special: `USER_HELD_SECRETS` in
`onbox_config.py`. They are generated **on-box** and **shown once** so the
client keeps a copy in their password manager, and they are NOT settable
through the settings config-write API (a restic re-key is a deliberate action).
The converge mints the admin and console passwords if absent, and the installer
shows them at the end of `catena-cli install` (`playbooks/show-keyset.yml`).
The restic password is `MINTED_ON_REQUEST`: the client generates it in the
panel beside the backup repository (`scripts/catena-restic-key.py generate`,
which refuses when a password exists or the repository already holds backups
under another one), and saves it there. To recover a lost server the client
installs Catena on a new one and enters the old repository with the saved
restic password in the panel's restore; the restore brings the old store back
with it, and the password that opened the repository stays. Generating the
restic password on-box is safe precisely because it is surfaced once off-box:
without that copy a lost box is unrecoverable, which is the client's
responsibility.

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
then set_facts them for the roles. `seed.py` mints none of these.

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
- `beszel_universal_token`

### 3. Service-minted, role-captured (`ROLE_MINTED_SECRETS`)

Minted by the service ITSELF, not by this repo and not by a human. The role
that provisioned the service captures the value and writes it into the same
on-box store, so it rides the backup like every category-2 secret -- but it is
neither generated by `onbox_config.py` nor accepted through the settings API,
because it has exactly one writer and that writer is the role.

| Key | Minted by | Captured by |
| --- | --- | --- |
| `portainer_api_key` | Portainer's own token API, via `helpers/bootstrap_portainer_admin.py` | `reconcile/roles/portainer` |

The mint runs mid-converge rather than in the loader: the initial admin has to
exist first. The helper prints the key on stdout and the role writes it
straight into the store, so it never touches a file off-box. The role re-mints
with `--overwrite` when the live Portainer rejects the stored key (a `/data`
restore replaces the BoltDB the token lived in), which is why fill-only adopt
is not sufficient here.

### 4. Non-secret config

One place to edit each value. A value catena-admin can change without
consequence lives in the on-box store only; the inventory `.env` keeps what
the installer needs to reach and install the server, and what is hard to
change afterwards.

**Inventory `.env` (the installer's):** `HOST_PUBLIC_IP`, `HOST_INITIAL_USER`,
`HOST_SSH_PORT`, `SSH_PRIVATE_KEY` (its `.pub` beside it), `OPS_USER`,
`STORAGE_BULK_*` (the bulk mount is applied by an operator-run role, so the
panel could not change it).

**Store, seeded from the `.env`:** `ADMIN_EMAIL`, `APT_PROXY_URL`,
`DOCKER_REGISTRY_MIRROR_URL` and the development-only ACME and staging keys.
The first converge needs them before the panel exists, so the `.env` seeds
them fill-only on that converge and is never read for them again.

**Store, catena-admin Settings only:** the domain (`CLOUDFLARE_ZONE`), the
subdomains, the tailnet (`TAILNET_PROVIDER`, `TAILNET_CONTROL_URL`,
`HEADSCALE_USER`, `TAILSCALE_TAGS`), backups (`BACKUP_*`), mail (`SMTP_*`),
notifications (`NTFY_*`), `IDENTITY_ENFORCE_MFA`, and the server's time zone
and locale (`COMMON_TIMEZONE`, `COMMON_LOCALE`). They have no `.env`
line; `catena-cli` names a filled one it finds, because nothing reads it.

The owners are DECLARED, in `helpers/knobs.yml` (a knob has an `env` entry, a
`panel` entry, or neither, never both), and projected by
`helpers/onbox_config.py`: `BOOTSTRAP_CONFIG` is the `.env`-owned set,
`SETTINGS_CONFIG` maps each store-owned key to the Ansible variable the
converge publishes it as, and `ENV_SEEDED_CONFIG` is the store keys the `.env`
seeds.

The published facts are prefixed `cfg_` rather than named after the consuming
variable. `reconcile/roles/keycloak` derives `smtp_host` from the Resend/Brevo autofill,
so a fact named `smtp_host` would have replaced the derivation with the raw
value -- silently, since a fact outranks a role default.

## On-box persistence target

`/etc/catena/` already rides the backup (`backup_paths` includes `/etc`) and
already holds `backup.env` (S3 creds) + `restic.pass` (restic password),
both written reconcile-not-overwrite by `reconcile/roles/backup`. That is the model
for every category-2 secret and category-4 value: a single on-box config
source-of-truth under `/etc/catena/`, written once, reconciled on converge,
carried in every snapshot. The controller-side inventory is non-secret only
(`.env`, `hosts.yml`); nothing writes a secrets file there.

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
