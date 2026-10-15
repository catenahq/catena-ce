# catena-admin (Ansible role)

Host-side setup AND container deploy for the per-VPS admin panel -- the
catena-admin Go shell. This role prepares everything the container
expects to find on the host, then creates the container itself as a
TIER-1 SWARM SERVICE, with the argv rendered by
[../../../playbooks/filter_plugins/catena_admin_service.py](../../../playbooks/filter_plugins/catena_admin_service.py).

## What this role does

- Renders the bash dispatch table (`/etc/catena/admin-actions`) that the
  dispatcher (`/usr/local/sbin/catena-admin-runner`) sources, from the
  canonical action catalog at [templates/actions.yml.j2](templates/actions.yml.j2)
  merged with per-inventory `catena_admin_extra_actions`. The runner
  account and the panel's SSH key are
  [../../../bootstrap/roles/catena_admin_host](../../../bootstrap/roles/catena_admin_host/README.md);
  the forced command, the sudo and sshd rules and the dispatcher itself are
  [../dispatch_path](../dispatch_path/README.md).
- Creates `/etc/catena/admin-actions.d/`, root-owned, and reads nothing
  from it. The dispatcher offers a name this table does not carry to the
  drop-ins there before refusing it, which is how the actions that
  ship in the panel image are reachable without being authorized
  from here. Each runs a binary the payload installs at a path the
  payload chose, so this repo would have been holding an authorization
  record for names it does not own -- and a one-line change to any of
  them would have needed a converge against a live client. The one action
  still authorized here is declared, with its reason, in
  [../../../tests/unit/test_converge_dispatch_table.py](../../../tests/unit/test_converge_dispatch_table.py).
- Renders the catalog the Go shell reads at runtime
  (`/etc/catena/admin-actions.yml`) from the same source, plus the
  parallel YAML allow-list at `/etc/catena/admin-allowed.yaml`
  (documentation + audit artifact).
- Deploys against the bind-mount targets the service
  ([../../../playbooks/filter_plugins/catena_admin_service.py](../../../playbooks/filter_plugins/catena_admin_service.py))
  expects: `/etc/catena/admin-ssh/`, `/etc/catena/admin-actions.yml`,
  `/etc/catena/extra-tiles.yml`, `/var/lib/catena/` (read-only stats;
  populated by catena-backup-run + catena-gatus-sync), and
  `/var/backups/catena-export/` (recovery artifacts, read-only). The
  shell's one writable directory is its own state,
  `/var/lib/catena/admin/`.
- Does NOT install the host payload (all engines --
  `catena-cloudflared-sync`, `catena-daily`, the lane scripts, units).
  `reconcile/roles/payload` does, earlier in the converge, straight out of
  the image -- two installers would race over `/usr/local/bin` and the
  loser would be whichever image was staler. It is ungated: even a plain
  Community host gets the engines
  (the Cloudflare tunnel engine especially, which
  `reconcile/roles/cloudflare_tunnel` dispatches). The license only gates which UI
  features the shell renders at runtime, never the install. The
  `ee-install-engines` reserved action re-installs out of band after an
  image bump, extracting from the running image as root, and it stays in
  this repo's table precisely because it is the way back when a drop-in
  is broken. It starts a detached unit that holds `/run/catena.lock`, so
  it waits for the panel's self-update and the host's own converge, which
  refill the same stage directory, and the button reports that it started.
- Renders `/etc/catena/extra-tiles.yml` from inventory
  `catena_admin_extra_tiles` (operator escape hatch for hand-authored
  Apps-tab tiles).
- Creates the catena-admin container as a tier-1 swarm service
  ([tasks/deploy.yml](tasks/deploy.yml)), pulling the PUBLIC GHCR image
  (`catena_admin_image`) anonymously -- every install gets the panel; the
  Pro and Business features inside it are gated at runtime by the license
  check. The image is `CATENA_ADMIN_IMAGE`, the one the converge was
  started with: the release install-host.sh installs, or the image the
  panel runs on a converge the host runs itself (catena-converge). The
  service is created against the local swarm directly because the panel
  is the tool that repairs the host, Portainer included, so it cannot
  depend on Portainer to start. The
  credentials arrive as swarm secrets, not `--env`: `docker service
  create` has no `--env-file`, so an `--env` value sits in the host
  process table where any local user can read it. One of them,
  `catena_admin_gate_secret`, is what the panel's sign-in gate sends with
  every request it forwards: the panel's listener is on `catena-network`,
  where any client app container can set identity headers, and it takes an
  identity only from a request carrying that secret. A secret of the panel's
  that no service mounts is removed after the deploy. The service
  also joins the private overlays of Healthchecks (`healthchecks_network`),
  to read its checks, of Beszel's hub (`beszel_network`), to read its API,
  and of Portainer (`portainer_network`), which reads its App Templates
  catalog here, and the spec's networks are reconciled like its env and
  secrets. No
  Traefik route is written here (oauth2-proxy owns the gated `dash.<zone>`
  route). The test bench renders its argv from the same filter plugin when it
  recreates a panel a scenario removed.

## What this role does NOT do

- It does **not** create a Keycloak realm client. The admin sits
  behind the shared `oauth2-proxy` realm client and the staff/admin
  oauth2-proxy slug.

## Community vs licensed actions

The canonical catalog ships only Community actions: manual backup +
snapshot browse/export, the per-app wiring buttons, and Ops
diagnostics. The **Upgrades** category is
intentionally empty in Community -- managed updates and the
catena-daily orchestrator are Pro lanes whose buttons are
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
