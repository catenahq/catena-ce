# catena-admin (Ansible role)

Host-side setup AND container deploy for the per-VPS admin panel -- the
catena-admin Go shell. This role prepares everything the container
expects to find on the host, then deploys the container itself as a
Portainer stack from
[files/catena-admin.compose.yml](files/catena-admin.compose.yml).

## What this role does

- Creates the `catena-admin-runner` system user with a forced-command
  SSH key. The admin container SSHes into this account to dispatch
  actions on the host; the forced command pins every connection to the
  dispatcher.
- Installs the dispatcher script
  (`/usr/local/sbin/catena-admin-runner`) and the bash dispatch table
  (`/etc/catena/admin-actions`) rendered from the canonical action
  catalog at [templates/actions.yml.j2](templates/actions.yml.j2)
  merged with per-inventory `catena_admin_extra_actions`.
- Renders the catalog the Go shell reads at runtime
  (`/etc/catena/admin-actions.yml`) from the same source, plus the
  parallel YAML allow-list at `/etc/catena/admin-allowed.yaml`
  (documentation + audit artifact).
- Generates an ed25519 keypair under `/etc/catena/admin-ssh/`
  (chowned for the container's uid 1000) and seeds known_hosts via
  ssh-keyscan.
- Creates the bind-mount targets the admin compose
  ([files/catena-admin.compose.yml](files/catena-admin.compose.yml))
  expects: `/etc/catena/admin-ssh/`, `/etc/catena/admin-actions.yml`,
  `/etc/catena/extra-tiles.yml`, `/var/lib/catena/` (read-only stats;
  populated by run-backup.sh + gatus-sync), and
  `/var/backups/catena-export/` (recovery artifacts, read-only). The
  shell's writable bind is `/var/lib/catena/ee-payload`, where it mirrors
  its embedded host payload (all engines -- `catena-cloudflared-sync`,
  `catena-daily`, the lane scripts, units) at startup.
- Installs that host payload into `/usr/local/bin` on **every** converge
  ([tasks/deploy.yml](tasks/deploy.yml)), ungated -- the payload is NOT
  license-gated. Even a plain Community host gets the engines (the
  Cloudflare tunnel engine especially: `roles/cloudflare_tunnel`
  dispatches `catena-cloudflared-sync`). The license only gates which UI
  features/buttons the shell renders at runtime, never the install. The
  `ee-install-engines` reserved action remains for out-of-band re-install
  after an image bump.
- Renders `/etc/catena/extra-tiles.yml` from inventory
  `catena_admin_extra_tiles` (operator escape hatch for hand-authored
  Apps-tab tiles).
- Deploys the catena-admin container as a Portainer stack
  ([tasks/deploy.yml](tasks/deploy.yml)) from
  [files/catena-admin.compose.yml](files/catena-admin.compose.yml),
  pulling the PUBLIC GHCR image (`catena_admin_image`) anonymously --
  every install gets the panel; the Business feature set inside it is
  gated at runtime by the license check. The image ref and every per-host
  value are supplied via the stack Env array (${VAR} substitution); no
  Traefik route is written here (oauth2-proxy owns the gated
  `dash.<zone>` route). The test bench drives the identical path but
  builds the image locally instead of pulling from GHCR.

## What this role does NOT do

- It does **not** create a Keycloak realm client. The admin sits
  behind the shared `oauth2-proxy` realm client and the staff/admin
  oauth2-proxy slug.
- It carries **no** audit.db quiesce hooks. The audit module is a
  license-gated Business capability; Community ships no audit log.

## Community vs Business actions

The canonical catalog ships only Community actions: manual backup +
snapshot browse/export, the per-app wiring buttons, and Ops
diagnostics. The **Upgrades** category is
intentionally empty in Community -- managed updates and the
catena-daily orchestrator are Business lanes whose buttons are
contributed at runtime by license-gated plugins (the Go shell merges
plugin `Actions()` into this catalog). An Upgrades / daily-chain button
therefore cannot appear without an active license.

## Variables

See [defaults/main.yml](defaults/main.yml). The two operator-facing
knobs are:

- `catena_admin_extra_actions` -- list of additional dispatcher
  entries to append to the canonical catalog. Same shape as
  [templates/actions.yml.j2](templates/actions.yml.j2).
- `catena_admin_extra_tiles` -- list of hand-authored launcher tiles
  the Go shell's Apps tab consumes.

`catena_admin_default_language` (`en` | `fr`) and
`catena_admin_default_theme` (`light` | `dark` | `system`) inherit
from the inventory's `catena_default_language` /
`catena_default_theme` if defined.
