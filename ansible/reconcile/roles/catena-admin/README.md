# catena-admin (Ansible role)

Host-side setup AND container deploy for the per-VPS admin panel -- the
catena-admin Go shell. This role prepares everything the container
expects to find on the host, then creates the container itself as a
TIER-1 SWARM SERVICE, with the argv rendered by
[../../playbooks/filter_plugins/catena_admin_service.py](../../playbooks/filter_plugins/catena_admin_service.py).

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
- Creates `/etc/catena/admin-actions.d/`, root-owned, and reads nothing
  from it. The dispatcher offers a name this table does not carry to the
  drop-ins there before refusing it, which is how the thirty-six actions
  that ship in the panel image are reachable without being authorized
  from here. Each runs a binary the payload installs at a path the
  payload chose, so this repo would have been holding an authorization
  record for names it does not own -- and a one-line change to any of
  them would have needed a converge against a live client. The four that
  are still authorized here are declared, with their reasons, in
  [../../tests/unit/test_converge_dispatch_table.py](../../tests/unit/test_converge_dispatch_table.py).
- Renders the catalog the Go shell reads at runtime
  (`/etc/catena/admin-actions.yml`) from the same source, plus the
  parallel YAML allow-list at `/etc/catena/admin-allowed.yaml`
  (documentation + audit artifact).
- Generates an ed25519 keypair under `/etc/catena/admin-ssh/`
  (chowned for the container's uid 1000) and seeds known_hosts via
  ssh-keyscan.
- Creates the bind-mount targets the service
  ([../../playbooks/filter_plugins/catena_admin_service.py](../../playbooks/filter_plugins/catena_admin_service.py))
  expects: `/etc/catena/admin-ssh/`, `/etc/catena/admin-actions.yml`,
  `/etc/catena/extra-tiles.yml`, `/var/lib/catena/` (read-only stats;
  populated by catena-backup-run + catena-gatus-sync), and
  `/var/backups/catena-export/` (recovery artifacts, read-only). The
  shell's writable bind is `/var/lib/catena/ee-payload`, where it mirrors
  its embedded host payload (all engines -- `catena-cloudflared-sync`,
  `catena-daily`, the lane scripts, units) at startup.
- Does NOT install that payload. `reconcile/roles/payload` does, earlier in
  the converge, straight out of the image -- two installers would race
  over `/usr/local/bin` and the loser would be whichever image was
  staler. It is ungated: even a plain Community host gets the engines
  (the Cloudflare tunnel engine especially, which
  `reconcile/roles/cloudflare_tunnel` dispatches). The license only gates which UI
  features the shell renders at runtime, never the install. The
  `ee-install-engines` reserved action remains for out-of-band re-install
  after an image bump, and it stays in this repo's table precisely
  because it is the way back when a drop-in is broken.
- Renders `/etc/catena/extra-tiles.yml` from inventory
  `catena_admin_extra_tiles` (operator escape hatch for hand-authored
  Apps-tab tiles).
- Creates the catena-admin container as a tier-1 swarm service
  ([tasks/deploy.yml](tasks/deploy.yml)), pulling the PUBLIC GHCR image
  (`catena_admin_image`) anonymously -- every install gets the panel; the
  Business feature set inside it is gated at runtime by the license
  check. The image is `CATENA_ADMIN_IMAGE`, the one the converge was
  started with: the release install-host.sh installs, or the image the
  panel runs on a converge the host runs itself (catena-converge). The
  service is created against the local swarm directly because the panel
  holds the key that drives Portainer and is the tool that repairs it, so
  it cannot depend on Portainer to start. The
  credentials arrive as swarm secrets, not `--env`: `docker service
  create` has no `--env-file`, so an `--env` value sits in the host
  process table where any local user can read it. One of them,
  `catena_admin_gate_secret`, is what the panel's sign-in gate sends with
  every request it forwards: the panel's listener is on `catena-network`,
  where any client app container can set identity headers, and it takes an
  identity only from a request carrying that secret. The service also joins
  the private overlays of Healthchecks (`healthchecks_network`), to read its
  checks, and of Beszel's hub (`beszel_network`), to read its API, and the
  spec's networks are reconciled like its env and secrets. No
  Traefik route is written here (oauth2-proxy owns the gated `dash.<zone>`
  route). The test bench renders its argv from the same filter plugin when it
  recreates a panel a scenario removed.

## What this role does NOT do

- It does **not** create a Keycloak realm client. The admin sits
  behind the shared `oauth2-proxy` realm client and the staff/admin
  oauth2-proxy slug.

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

`catena_admin_default_theme` (`light` | `dark` | `system`) inherits
from the inventory's `catena_default_theme` if defined. The panel's
language is not an inventory setting: it starts in English and each
visitor switches with the header toggle.
