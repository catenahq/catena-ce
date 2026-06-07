# catena-admin (Ansible role)

Host-side setup for the per-VPS admin panel -- the catena-admin Go
shell. The container itself is deployed via Dokploy's git-source
compose flow; this role manages everything the container expects to
find on the host before it boots.

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
  ([deploy/catena-admin/dokploy.compose.yml](../../../deploy/catena-admin/dokploy.compose.yml))
  expects: `/etc/catena/admin-ssh/`, `/etc/catena/admin-actions.yml`,
  `/etc/catena/extra-tiles.yml`, `/var/lib/catena/` (read-only stats;
  populated by run-backup.sh + gatus-sync), and
  `/var/backups/catena-export/` (recovery artifacts, read-only). The
  shell's writable state is the `admin-plugins` named volume at
  `/var/lib/catena/plugins`, where the license-gated pull lands EE plugin
  binaries; Community runs with it empty.
- Renders `/etc/catena/extra-tiles.yml` from inventory
  `catena_admin_extra_tiles` (operator escape hatch for hand-authored
  Apps-tab tiles).

## What this role does NOT do

- It does **not** push the catena-admin compose via the Dokploy API.
  The container is deployed through Dokploy's git-source flow pointed at
  [deploy/catena-admin/dokploy.compose.yml](../../../deploy/catena-admin/dokploy.compose.yml)
  (which builds the repo-root Dockerfile). The test bench reuses that same
  compose, building the image on the VPS instead of via git-source.
- It does **not** create a Keycloak realm client. The admin sits
  behind the shared `oauth2-proxy` realm client and the staff/admin
  oauth2-proxy slug.
- It carries **no** audit.db quiesce hooks. The audit module is a
  license-gated Business capability; Community ships no audit log.

## Community vs Business actions

The canonical catalog ships only Community actions: manual backup +
snapshot browse/export, the per-app wiring buttons, recovery-archive
generation, and Ops diagnostics. The **Upgrades** category is
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
