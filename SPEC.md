# SPEC.md

Catena-CE is an Ansible-based installer for Catena. It prepares a new Debian 13 server to host applications that run in Docker containers, as a secure platform that is ready for deployments. The main dashboard, `catena-admin`, is built from a closed-source repository, and its image is available [on Github](https://github.com/catenahq/catena-admin/pkgs/container/catena-admin).

This file is the contract, and the code follows it: the repository implements
what this file states. Each section states its scope in a few lines and its
behaviour as invariants, statements that always hold, each with the gates that
prove it ([Gate pointers](#gate-pointers)). An invariant marked **no bench yet**
has no rehearsal scenario that proves it; a scenario that proves no invariant is
listed in [Scenarios outside the invariants](#scenarios-outside-the-invariants).
Items this file declares that the repository does not implement yet are listed
in [Declared, not yet built](#declared-not-yet-built).

## Not in this repository

- **The dashboard source.** `catena-admin` is closed source. This repository
  deploys its public, signed image and extracts the host engines, the programs
  that run on the server itself, from that same image.
- **The application catalogue.** The compose files and templates of each
  application live in `catena-templates`.
- **Maintainer tooling.** The rehearsal bench, the audit graph and the
  attestation are not published.
- **Secrets.** The repository contains none. What a deployment needs is
  documented in [ansible/SECRETS.md](ansible/SECRETS.md).

## Licensing

Catena-CE is complete on its own. Catena Pro and Catena Business are
subscriptions sold through Polar; each comes with a key that is saved in the
dashboard and unlocks dashboard features. The website's
[edition comparison](https://catena.run/en/#pricing) lists which features.

| Invariant | Enforced by |
| --- | --- |
| A server with no key, or with a key that Polar refuses, runs as Community, and everything Community offers keeps working | `bench:activate_ee#stage-1.5-negatives`, `bench:ee_lapse#stage-4-freeze`, `bench:license_seat#stage-6-a-fails-open`, `threat:LE3` |
| A Pro key unlocks the Pro features and no Business feature: the Business pages answer "not found" and the server refuses the Business actions | `bench:ee_entitlement_partial` |
| The server checks its key with Polar every hour, and only while it has a key saved | **no bench yet** |
| A key is active on only one server at a time and is tied to that server's hardware: while the first server holds the key, a copy of that server and a second server are both refused | `bench:license_seat#stage-2-a-copy-is-refused`, `bench:license_seat#stage-3-a-second-server-is-refused`, `bench:polar_sandbox_smoke#stage-4-second-server-refused`, `threat:LE7` |
| When Polar records an activation of the key for a server but Polar's reply never reaches the server, the activation is labelled in Polar with the server's domain and the server's licence status names the activation that went unanswered; once that activation is freed in Polar and the key is saved again, the server is licensed | `bench:license_seat#stage-4.6-an-unanswered-activate-is-named` |
| When Polar holds an activation of the key that the server does not know about, the dashboard says to free that activation in Polar's customer portal | **no bench yet** |
| While Polar cannot be reached, a licensed server keeps its paid features for 48 hours after Polar last confirmed its key, then locks them, and unlocks them once Polar answers | `bench:polar_unreachable_grace`, `threat:LE3` |
| A server whose key is revoked in Polar, or whose activation is freed in Polar, locks its paid features at its next check, with no grace period | `bench:ee_lapse#stage-2-revoked-at-polar`, `bench:license_seat#stage-5-the-first-locks` |
| A server whose key is cancelled in Polar locks its paid features at its next check, with no grace period | **no bench yet** |
| When a server locks its paid features, the admin is told once: by one entry in the maintenance log and by one email sent through the server's outgoing mail | `bench:ee_lapse#stage-5-one-lock-notice`, `bench:polar_unreachable_grace#stage-3-past-the-grace` |
| A server that locks its paid features keeps everything else: the scheduled backups and vulnerability checks, the OS updates, restores and the Community actions keep working | `bench:ee_lapse#stage-3-host-lapsed`, `bench:license_seat#stage-6-a-fails-open`, `threat:LE3` |
| Community actions keep working on a licensed server, and they leave its licence state unchanged | `bench:ee_ce_regression#stage-2-press-ce-action`, `bench:ee_ce_regression#stage-4-business-still-active` |
| The version of the Polar API that the licence check is pinned to still answers in Polar's sandbox | `bench:polar_api_version_current#stage-1-pinned-version-answers` |
| On a server whose paid features are locked, the applications keep running with their data, and a backup, a restore or an update started by hand still runs | **no bench yet** |
| Migrating a server to a new machine does not need a paid edition on the new machine | `bench:wizard_migrate_round_trip#stage-0-preconditions` |
| Migrating a server that has no paid edition to a new machine is refused | **no bench yet** |
| During a migration, the new server reaches the old one over SSH only, using a one-time migration ticket and the pairing code that the old server's dashboard shows, and only while the old server's migration window is open; no port is opened and no private network is needed | `bench:migrate_lane_auth_denied`, `bench:wizard_migrate_round_trip`, `threat:CV15` |
| During a migration, the licence key moves only once the migration is validated: the old server frees the key and closes its migration window, then the new server takes the key and runs a converge, the run that brings a server to what Catena declares | `bench:migrate#stage-1-migrate`, `bench:wizard_migrate_round_trip#stage-4-window-and-completion`, `bench:migrate_lane_auth_denied#stage-4.5-licence-release-guarded`, `bench:migrate_preseed_no_split_brain#stage-5-licence-stays-with-the-source`, `bench:wizard_migrate_resume_source`, `threat:LE12` |
| Error reports leave a server of any edition only while its admin has switched them on, contain only the check, its state and the application it concerns, are listed in the dashboard, and stop at once when they are switched off; a server without a paid subscription registers with a random install identifier and sends no licence data | `bench:error_reports_opt_in`, `bench:error_reports_opt_in#stage-6-a-community-server-enrols-with-an-install-identity`, `threat:CV18` |
| The text of a failed job reaches support only from a paid server, only after the admin has reviewed it, and only as the admin edited it | `bench:error_reports_opt_in` |
| The dashboard offers the text of a failed job for download | **no bench yet** |
| The dashboard image carries the licence text of every third-party Go module compiled into it, and a module that is added or changed without its licence text fails the build | **no bench yet** |

## How this repository is layered

The installer reaches the server over SSH, and the server runs
`ansible/install-host.sh` from the release image's own tree. Each file at the
top of `ansible/playbooks/`, such as `converge.yml`, is one complete operation:
the servers it targets and an ordered list of roles to apply. A role is the
unit that gets reused: it has its own defaults, templates, handlers and
`validate.yml`. A task is a single step, and one task serves every place that
needs that same step. Reuse runs in one direction only: an operation reuses
roles, and a role reuses tasks. A converge is a run of `converge.yml` or
`reconcile.yml`: it applies the roles and changes only what differs from what
they declare.

## The installer

`catena-installer` (`installer/`) runs on the client's machine with uv
(`uvx catena-installer`), and it does only what reaching and installing a
server takes: SSH access, the admin email, and what the first converge (the run
that sets the server up as the roles declare) needs. Everything the dashboard
can change is set in the dashboard. The installer's commands are `init`,
`install`, `connect` and `uninstall`; `ansible/seed.py` writes the inventory.
`install` runs on the server in two steps, each through its own SSH session:
the accounts step creates the `ops` and `panel` accounts, and the system step,
logged in as `ops`, runs the rest of the install.

| Invariant | Enforced by |
| --- | --- |
| On Windows, macOS and Linux, the installer started with no arguments serves a page on the machine's loopback address, and the installer started with a command works in the terminal | `workflow:ci.yml#installer`, `workflow:ci.yml#entry-points`, **no bench yet** |
| The installer runs no Ansible itself: the server installs itself from the tree of the release image it fetched, so the server's roles, engines and dashboard all come from one release | `bench:update_from_previous_release#stage-4b-one-version-after-the-reinstall`, `bench:install_rerun#stage-4-engines-follow-the-shell`, `workflow:ci.yml#installer` |
| The installer asks only for what reaching and installing a server needs, and an install that is given a value the dashboard owns stops before writing anything | `workflow:ci.yml#installer`, **no bench yet** |
| `init` writes an inventory in which every key is at its default value | `workflow:ci.yml#installer`, **no bench yet** |
| `install` connects to the server at its SSH address when one is set, and otherwise at its public IP address; `--address` connects to another address for that one run | `bench:launcher_install#stage-1-private-leg-installs`, `bench:launcher_install#stage-3-public-leg-installs`, `bench:install_rerun#stage-1a-address-is-for-this-run` |
| `install` run a second time asks nothing, changes nothing on a healthy server, and deploys the dashboard again when the dashboard is missing | `bench:install_rerun#stage-1-install-rerun`, `bench:install_rerun#stage-6-install-deploys-the-panel` |
| `install` run a second time re-applies the release the server records, including the bootstrap roles (the roles that only the install applies) | **no bench yet** |
| `install --release` moves the server to that release, running that release's code | **no bench yet** |
| `install --tags` limits the install's converge to the roles named | **no bench yet** |
| A server that can be reached only through the login its hosting provider gave installs in two steps: the accounts step creates the `ops` and `panel` accounts with the installer's key and turns off no existing login, then the system step runs as `ops` | `bench:launcher_install#stage-1-private-leg-installs` |
| The system step of the install, whose first role turns off every other way to log in, starts only once the SSH server has logged a session opened as `ops` with the installer's key | `workflow:ci.yml#installer`, **no bench yet** |
| An install step whose SSH connection drops still runs to its end on the server, and the server's log of that step contains no password | `bench:fi_o2_operator_ctrl_c#stage-1b-interrupted-install-ends-on-the-server` |
| An install started while a step of an earlier install still runs on the server waits for that step to end | `workflow:ci.yml#installer`, **no bench yet** |
| After the system step, the installer scans every TCP port of the server's public IP address, compares the open ports with the ports the server declares, and checks that the scan cannot reach the containers' network | `workflow:ci.yml#installer`, **no bench yet** |
| When a server presents a different SSH host key from the one trusted for its address, `install` stops before anything reaches the server; `--reinstalled`, or the reinstalled-server box on the installer's page, forgets the old key and trusts the new one | `bench:launcher_install#stage-6-install-stops-on-the-new-key`, `bench:launcher_install#stage-7-reinstalled-flag-continues`, `bench:launcher_install#stage-8-new-key-trusted` |
| The inventory contains no credential | `bench:launcher_install#stage-2-private-leg-leaves-its-answers`, `bench:launcher_install#stage-4-public-leg-leaves-its-answers` |
| An admin password reaches the server as a file with mode 0600 that exists only for the length of the run, and the installer generates no secret | `workflow:ci.yml#installer`, **no bench yet** |
| The dashboard is reached through an SSH port forward, made with the `panel` account, to the server's loopback address | `bench:ce_install_suite#stage-3-bare-install` |
| `connect` opens the `panel` account's port forward to the dashboard from the client's machine | `workflow:ci.yml#installer`, **no bench yet** |
| `uninstall` hands the server back to Debian: Debian's package updates run with Debian's default settings again, and no container engine package stays locked at its version | `bench:ce_uninstall#stage-3-handback-asserts` |
| `uninstall` prints instructions for tearing down what remains | **no bench yet** |
| An installer command that runs a single file from `ansible/playbooks/` has that file's name | `audit:check-grid` |

## Playbooks

At install, the server runs `accounts.yml`, `bootstrap.yml`, `show-keyset.yml`,
`converge.yml` and `validate.yml`. The server runs `reconcile.yml` when it
converges itself, that is, when it brings itself back to what the roles
declare. It runs `lockdown.yml`, `rotate-tunnel.yml` and `rotate-tailscale.yml`
only when they are started from the dashboard. `uninstall.yml` hands the server
back to Debian.

| Invariant | Enforced by |
| --- | --- |
| A converge run again repairs a server that drifted away from what the roles declare, and never duplicates or breaks anything on a healthy server | `bench:install_rerun#stage-1-install-rerun`, `bench:converge_suite#stage-7c-rotated-secret-arrives`, `bench:converge_suite#stage-7b-bump-survives` |
| The server converges itself from the Ansible tree inside the dashboard image | `bench:reconcile_is_a_noop#stage-1-first-reconcile` |
| The server's own converge reads nothing from an inventory on the client's machine, and no installer command starts it | `workflow:ci.yml#installer`, **no bench yet** |
| `reconcile.yml` applies the roles of `converge.yml` in the same order, except the bootstrap roles, which only an install started by a person applies | `workflow:ci.yml#installer`, **no bench yet** |
| Validation checks the state of every role and, from the server itself, checks the dashboard, the public hostnames once a domain is set, and the published ports; a converge ends with validation and fails when validation fails; validation judges the settings the converge started with, so a setting saved while a converge runs is judged by the converge that applies it | `bench:ce_validate#stage-1-ce-validate`, `bench:ce_install_suite#stage-3a7a-the-private-network-saved-during-a-domain-apply` |
| Validation fails when the server exposes something it does not declare | **no bench yet** |
| Validation is the last step of the install, and it can also be run by itself | `workflow:ci.yml#installer`, **no bench yet** |
| `lockdown.yml` joins the server to the private network saved in the dashboard | `bench:ce_install_suite#stage-3a1-tailnet-joined`, `bench:fi_n11_lockdown_refuses_shields_up` |
| `lockdown.yml` runs only when started from the dashboard, and never during a converge | `workflow:ci.yml#installer`, **no bench yet** |
| The install generates the admin and console passwords when they do not exist yet, and shows them, together with the key that verifies the system journal, right after its bootstrap step and again when it ends | **no bench yet** |
| An admin can make the server sign in to its private network again from the dashboard | **no bench yet** |
| A converge that meets a hung container engine or a badly degraded network fails within a bounded time and names the cause | `bench:fi_c1_docker_daemon_hang`, `bench:fi_c4_registry_pull_timeout` |
| A converge on a server whose disk is nearly full, whether started by the install or from the dashboard, stops at its disk check before any role runs | `bench:fi_d5_disk_full_mid_converge` |

## Roles

A role is **Bootstrap** when nothing else could repair it if it were wrong: the
install applies it, on the server, from the release image, and this set of
roles stays small. Every other role is **Reconcile**: what it sets depends on
the Catena release or on the server itself, and the server runs it unattended
from the image. A role's directory shows which kind it is:
`ansible/bootstrap/roles/` or `ansible/reconcile/roles/`. `ansible/boundary.yml`
lists each role, the reason each bootstrap role is bootstrap, and how the roles
outside a converge's list of roles are run.

Bootstrap: `common`, `host_hardening`, `tailscale`, `storage`, `docker`,
`catena_admin_host`, `ansible_runtime`. Reconcile, in the order a converge
applies them: `dispatch_path`, `payload`, `public_ports`, `host_maintenance`,
`swarm`, `traefik`, `postgres`, `cloudflare_tunnel`, `keycloak`,
`oauth2_proxy`, `infrastructure`, `catena-admin`, `backup`. `tier1_stack` runs
after them, at the end of a converge; `cloudflare_tunnel_regenerate` runs only
from `rotate-tunnel.yml`.

| Invariant | Enforced by |
| --- | --- |
| Every role is either bootstrap or reconcile, never both, and every bootstrap role states the reason it is bootstrap | `audit:check-grid`, `workflow:ci.yml#installer` |
| No reconcile task writes a path that bootstrap owns (`/etc/ssh/`, `/etc/sudoers.d/`, `authorized_keys`, `/usr/local/sbin/catena-admin-runner`, `/usr/local/lib/catena-dispatch/`, `/etc/sysctl.d/`, `/etc/modprobe.d/`, `/etc/ansible/facts.d/`) | `workflow:ci.yml#installer` |
| No reconcile play reaches a bootstrap task file | `workflow:ci.yml#installer` |
| Only the `dispatch_path` role changes how the dashboard's requests reach the server, and only after proving that the new way works | `workflow:ci.yml#installer` |
| No bootstrap task writes a path that reconcile owns: the OS update and restart settings, the settings files for the journal and the clock, the configuration of the scheduled updates, the dashboard's declaration of its ports, and the job that reattaches containers to their overlay network | `workflow:ci.yml#installer` |
| A restore runs only when started on the server, never from the installer and never during a converge | `workflow:ci.yml#installer`, `bench:ce_restore#stage-4-ce-restore` |
| The install's converge and the server's own converge produce the same server: after either one runs, the other changes nothing | `bench:reconcile_is_a_noop` |
| A server that converges itself puts back the upkeep settings it owns: unattended upgrades, where the journal is stored, the mail agent kept off port 25, the clock on its NTP pool, how much task history Docker Swarm keeps, the label that marks the node holding data, and the configuration its scheduled jobs read | `bench:reconcile_repairs_host_upkeep#stage-3-restored`, `bench:reconcile_repairs_host_upkeep#stage-6-clock-on-the-pool` |
| The server resolves its own hostname, keeps the time zone and locale set in the dashboard, and runs git as the `catena-git` user, which has no login and no home directory | **no bench yet** |
| Kernel and kernel module hardening is in force | `bench:ce_install_suite`, `bench:ce_validate#stage-1-ce-validate` |
| Kernel hardening is applied before the container engine starts for the first time | **no bench yet** |
| Data lives under a single data directory on the server's own disk | `bench:ce_install_suite` |
| A remote network share mounted for bulk data holds the data the server assigns to it | **no bench yet** |
| The container engine runs the version this repository pins, and the server locks the engine at that version so package updates do not change it | `bench:ce_uninstall#stage-1-mask-apt-timers`, `bench:docker_engine_upgrade_round_trip#stage-3-the-new-engine-holds` |
| The dashboard sends its requests to the server by signing in as the runner account, with its own key pair and a host key it trusts; the account that migrations use accepts no key unless a migration window is open | `bench:ce_admin_smoke#stage-1-host-state`, `bench:migrate_lane_auth_denied#stage-0-preconditions`, `bench:migrate_lane_auth_denied#stage-1-no-lane-before-arming`, `bench:wizard_migrate_resume_source` |
| A server whose Ansible runtime broke repairs it without anyone stepping in | **no bench yet** |
| A dashboard update changes how the dashboard's requests reach the server only after a request has gone through the new way; when that check fails, the converge stops and the current way stays in use | `bench:update_from_previous_release#stage-3e-the-dispatch-path-moved-beside-the-running-one`, `bench:update_from_previous_release#stage-3f-a-failed-proof-keeps-the-path` |
| A converge installs the host engines only from the dashboard image it names, identified by digest and built from this repository's tree, and the root account runs nothing from a place the dashboard can write to | `bench:update_from_previous_release#stage-5-another-tree-is-refused`, `bench:ce_install_suite#stage-3d-payload-digest-gate`, `workflow:ci.yml#installer`, `threat:CV14` |
| A converge fails when the host engines it installs lack the program that applies the firewall rules for the server's published ports, or that program's timer | **no bench yet** |
| After the container engine restarts, containers that came up without their overlay network rejoin it by themselves | `bench:swarm_overlay_selfheal` |
| A deleted container starts again by itself; a removed service stays removed until the next converge, and that converge restores every declared service | `bench:container_delete_recreated#stage-1-app-task-rescheduled`, `bench:container_delete_recreated#stage-2-infra-task-rescheduled`, `bench:container_delete_recreated#stage-3-removed-service-stays-removed`, `bench:container_delete_recreated#stage-4-converge-restores-service`, `bench:container_delete_recreated#stage-5-declared-set-serves-again` |
| A server restarted without warning brings every declared service back, without a converge and without anyone stepping in | `bench:host_reboot_recovery#stage-3-host-booted`, `bench:host_reboot_recovery#stage-4-stack-back-without-a-converge`, `bench:host_reboot_recovery#stage-5-proxy-serves-again` |
| The single sign-on service keeps its database on the Postgres that serves the server's own services | **no bench yet** |
| Each admin tool that requires signing in has its own sign-in check and its own sign-in session | `bench:app_container_cannot_claim_an_identity#stage-5-a-seen-cookie-opens-no-admin-tool` |
| When the on-box store (`/etc/catena/config.json`, the server's settings file) holds no Cloudflare token, the tunnel waits and the converge succeeds | `bench:ce_install_suite#stage-3-bare-install`, `bench:install_without_a_domain#stage-1-converge-with-no-domain`, `bench:post_compromise_rebuild#stage-6-tunnel-sync-without-a-token` |
| The Cloudflare tunnel is replaced only when an admin asks for it, and a converge leaves a healthy tunnel connector running and untouched | `workflow:ci.yml#installer`, `bench:cf_tunnel_regenerate_round_trip#stage-1-rotate-tunnel`, `bench:fi_c6_cloudflared_flapping#stage-1-reconverge-no-flap` |
| The dashboard runs the release that the install fetched or, when the server runs its own converge, the image the dashboard already runs | `bench:update_from_previous_release#stage-4b-one-version-after-the-reinstall`, `bench:install_rerun#stage-4-engines-follow-the-shell` |
| A converge that does not name a dashboard image stops before any role runs | **no bench yet** |
| An install fetches the release it is given, otherwise the release the server records, otherwise the newest release the registry lists | `workflow:ci.yml#installer`, **no bench yet** |
| The first backup runs as soon as a backup destination is saved | `bench:ce_install_suite#stage-4i0-first-backup-ran` |
| At the end of a converge, the compose file built from the definitions of the control-plane services loads in the installed container engine and lists the services that run, and nothing is deployed from that file | `bench:ce_install_suite`, `workflow:ci.yml#installer` |
## State the tasks produce

Six things stay on the server after the run that wrote them has ended. Each one
belongs to the role or task that produces it.

### The on-box store -- `common`, read by every role

The on-box store is the file `/etc/catena/config.json` on the server, and it is
the one place the running server reads its values from. It holds the admin
password, every setting and credential saved in the dashboard, and every
internal secret, which the server generates itself.
`ansible/helpers/onbox_config.py` declares which of its names are secrets.

| Invariant | Enforced by |
| --- | --- |
| The on-box store can be read by root only (file mode 0600), and it holds every secret | `bench:ce_install_suite#stage-3b-onbox-store`, `threat:CV3` |
| Every backup holds the on-box store, and a whole-server restore brings it back | `bench:dr_suite#stage-19-onbox-store` |
| A client who has lost the install inputs can rebuild a usable set of credentials by reading the on-box store of the running server | `bench:recover_secrets_from_running_host` |
| An attacker who gains root on one server finds there no credential that reaches another Catena server: that server's SSH keys are refused on the other server, and no generated secret is the same on both | `bench:cross_host_pivot_finds_nothing#stage-2-ssh-pivot`, `bench:cross_host_pivot_finds_nothing#stage-3-store-custody`, `bench:cross_host_pivot_finds_nothing#stage-4-keycloak-master` |
| The keys of the backup's object storage appear on no dashboard page, and in no file or log on the server except the on-box store and the env file written from it | `bench:s3_backup_keys_rotation_round_trip#stage-6-no-secret-shown` |
| A converge (a run that brings the server back to what Catena declares) that is limited to some roles reads every value from the on-box store, and a value left in the inventory changes nothing | `bench:fi_s3_placeholder_inert#stage-1-placeholder-inert-converge-succeeds`, `bench:fi_s3_placeholder_inert#stage-2-onbox-store-not-poisoned`, `bench:converge_suite#stage-7c-rotated-secret-arrives` |
| The inventory fills the on-box store only at the first install; after that, the settings of the development certificate authority (`CATENA_ACME_*`) are the only values that every install copies from the inventory into the store | `bench:install_rerun#stage-8f-store-and-hosts-follow-the-env`, `bench:fi_s3_placeholder_inert#stage-2b-onbox-config-not-reseeded` |

### The public-port registry -- `public_ports`

Every port is declared before it opens: the roles write small files into
`/etc/catena/public-ports.d/`, application templates carry `vps.expose.*`
labels, and `catena-public-ports` merges both into the firewall rules (ufw and
the DOCKER-USER chain). Web traffic reaches the server only through the
Cloudflare tunnel. The install sets no domain, tunnel or private network; an
admin saves each of them in the dashboard.

| Invariant | Enforced by |
| --- | --- |
| The server's public IP address has no web port open, and no port is open outside the allowed set | `bench:ce_install_suite`, `bench:dr_suite#stage-14-post-recovery-assertions`, `threat:CV1` |
| A scan from outside of the server's public hostnames and IP address finds no critical or high vulnerability | `bench:security_scan#stage-7-emit-status` |
| Every open port is declared: the files the roles write and the labels of the application templates are merged into the ufw and DOCKER-USER rules, and both the validation and the scan from outside check the server against that merged list | **no bench yet** |
| The ports an application declares open when the application is deployed and close when it is removed | `bench:host_network_app_ports#stage-2-ports-open`, `bench:host_network_app_ports#stage-7-removal-closes-the-ports` |
| The labels in which an application declares its ports are read with the label names of the dashboard image the server runs, so an update of the dashboard image changes how those labels are read without a converge | `bench:labels_vocabulary_from_image` |
| The port rules are applied again every time the container engine starts, without waiting for the host lock that a converge or a scheduled job holds while it changes the server | **no bench yet** |
| SSH accepts keys only, with no root login and no password login | `bench:ce_install_suite`, `threat:CV6` |
| When no private network is set, SSH port 22 is open on the public IP address and nothing closes it | `bench:ce_install_suite#stage-3-bare-install`, `bench:ce_install_suite#stage-3-lockdown-without-a-tailnet-keeps-22` |
| When the server has joined a private network, SSH port 22 stays open on the public IP address until the Lockdown, the dashboard action that closes it | `bench:ce_install_suite#stage-3a2-end-state` |
| The Lockdown closes SSH port 22 on the public IP address only once the private network has proven that it reaches the server | `bench:ce_install_suite#stage-3a4-lockdown-closes-22`, `bench:fi_n1_tailnet_partition_mid_converge`, `bench:fi_v3_tailscale_acl_misconfig`, `bench:fi_n11_lockdown_refuses_shields_up` |
| After a Lockdown, SSH port 22 opens again on the public IP address while the private network is down | `bench:ce_install_suite#stage-3a6-tailnet-down-reopens-22` |
| Once the private network is set back to none and the change is applied, SSH port 22 opens on the public IP address | `bench:ce_install_suite#stage-3a7-no-provider-reopens-22` |
| An admin's open SSH session stays connected while the firewall rules are applied | `bench:fi_n8_ufw_concurrent_ssh` |
| A server installs with no domain, tunnel or private network, and a domain saved later in the dashboard brings the server's public URLs up through the tunnel without a reinstall | `bench:ce_install_suite#stage-3-bare-install`, `bench:ce_install_suite#stage-3a-tunnel-live`, `bench:install_without_a_domain` |
| Applying a domain from Settings shows its progress in the Domain section, a second Apply while one is running says that one is already running, and the section shows Applied without a page reload once the settings are applied | `bench:ce_install_suite#stage-3a7a-the-private-network-saved-during-a-domain-apply` |
| The server checks a credential entered in the dashboard before storing it, and never stores a credential the check refuses | `bench:ce_install_suite#stage-3a3-revoked-token-refused`, `bench:fi_s4_tailscale_oauth_revoked#stage-1-f-s4-save-refused`, `bench:s3_backup_keys_rotation_round_trip#stage-2-pair-b-saved-in-settings` |
| A Cloudflare token that Cloudflare refuses makes the converge fail at the tunnel role | `bench:fi_n4_cf_zone_misconfigured` |
| Until a domain is set, the dashboard is reached through the `panel` account, an SSH account that can only forward a port to the dashboard | `bench:ce_install_suite#stage-3-bare-install` |
| When an admin replaces the tunnel from the dashboard of a running server, with the Cloudflare token already in the on-box store, the server creates a new tunnel, points the wildcard DNS name at it, deletes the old tunnel and moves the tunnel connector onto the new tunnel's credential | `bench:cf_tunnel_regenerate_round_trip#stage-1-rotate-tunnel` |
| A Community server serves one domain: when it is given more, it serves the primary domain, keeps the other domains stored, and its converge still succeeds | `bench:fi_n10_multidomain_cap` |
| A Catena Pro server serves every extra domain listed in the Domain section of the dashboard, each with its own Cloudflare token, through the same tunnel | `bench:ee_multidomain#stage-1-attach-second-zone`, `bench:ee_multidomain#stage-3-single-tunnel` |
| When a Cloudflare token gives access to several domains, only the domains chosen in the dashboard are added | **no bench yet** |
| Removing an extra domain in the dashboard takes down its wildcard DNS name, and the server deletes that domain's token once the name is gone | `bench:ee_multidomain#stage-5-removed-domain-retires-its-wildcard` |
| Without Catena Pro, the dashboard adds no domain beyond the first one, and it still removes the extra domains a server kept | **no bench yet** |
| When Cloudflare refuses the token of an extra domain, that domain is left out when the converge sets up the tunnel, and the other domains are still served | `bench:ee_multidomain#stage-6-a-refused-extra-token-leaves-the-rest-served` |
| The dashboard refuses an extra domain that belongs to a different Cloudflare account from the first domain | **no bench yet** |
| Changing the domain in the dashboard moves the server onto the new domain: the new domain serves the server, the old domain's tunnel rule, wildcard DNS name and token are removed, the sign-in page and the applications move to the new domain, and a hostname that someone set by hand for the sign-in page is kept | `bench:primary_domain_change_round_trip#stage-3-the-move-is-a-move`, `bench:primary_domain_change_round_trip#stage-4-the-zone-list-names-the-new-domain`, `bench:primary_domain_change_round_trip#stage-4b-the-sign-in-page-names-the-new-domain`, `bench:primary_domain_change_round_trip#stage-5-the-new-domain-serves`, `bench:primary_domain_change_round_trip#stage-6-the-app-follows`, `bench:primary_domain_change_round_trip#stage-7-a-hand-set-sign-in-name-stays` |
| A server can join a private network whose control server is self-hosted and set in the dashboard, in place of the hosted control server | `bench:ce_install_headscale` |

### The restic repository -- `backup`

Backups are encrypted on the server and go to a restic repository in an S3
bucket set in the dashboard. restic and rclone ship with the host engines, the
programs the server extracts from the dashboard image. Restores run on the
server through `catena-recovery`, which the dashboard's restore page drives. On
Catena Business, the offsite copy copies the repository's objects into a locked
bucket (a bucket whose objects cannot be changed or deleted) at another
provider.

| Invariant | Enforced by |
| --- | --- |
| Backups are encrypted on the server before they are uploaded | `bench:dr_suite#stage-9-backup-and-pin`, `bench:rebuild_backs_up_after_restore#stage-2-a-password-of-its-own`, `threat:CV4` |
| The server generates the backup password once the backup destination and its keys are saved | `bench:ce_install_suite#stage-4e-configure-backup` |
| The backup password is shown once | **no bench yet** |
| A server can be rebuilt on a new machine from nothing but the backup destination and the set of keys kept off the server | `bench:dr_suite#stage-13-disaster-recovery`, `bench:rebuild_backs_up_after_restore#stage-4-store-holds-the-keysets-password`, `threat:CV9` |
| Saving a backup destination creates the repository when the bucket is empty, and is refused when the bucket holds other files; a backup never creates a repository | `bench:backup_destination_creates_repository#stage-3-save-creates-the-repository`, `bench:backup_destination_creates_repository#stage-2-save-refused-onto-other-files`, `bench:backup_destination_creates_repository#stage-4-backup-onto-an-emptied-bucket-exits-8` |
| A backup stops when the repository's password is wrong | **no bench yet** |
| A backup that cannot finish, whether because the disk is full, the object storage cannot be reached or the object storage refuses the key, fails and never reports success | `bench:fi_d4_disk_full_mid_snapshot`, `bench:fi_n6_s3_endpoint_5xx`, `bench:fi_n7_restic_repo_unreachable`, `bench:fi_s8_s3_hot_revoked` |
| A scheduled backup that fails shows as failed on the backup's alert check | `bench:fi_n6_s3_endpoint_5xx`, `bench:fi_s8_s3_hot_revoked` |
| Replacing the keys of the backup's object storage in the dashboard moves every backup onto the new key pair at once, so the old pair can be revoked without any backup failing | `bench:s3_backup_keys_rotation_round_trip#stage-3-backup-on-pair-b`, `bench:s3_backup_keys_rotation_round_trip#stage-5-backup-after-the-revocation` |
| An admin can list, browse and export backups without restoring them | `bench:inplace_restore_suite#wizard-restore-stage-2-page-lists-backups`, `bench:pitr_fuse_round_trip#stage-5-browse-from-the-panel`, `bench:snapshot_export_round_trip#stage-3-extract-canary` |
| Before each snapshot, every running Postgres database server is exported as plain SQL, one file per database server, and a failed export fails the backup | `bench:backup_quarter_hourly#stage-6-only-the-change-is-uploaded`, `bench:fi_b2_pg_dump_failed#stage-3-abort-before-restic` |
| Before each snapshot, every running MariaDB database server is exported as plain SQL, one file per database server, and a failed export fails the backup | **no bench yet** |
| A Postgres database server built on another image, such as pgvector, PostGIS or TimescaleDB, is exported and replayed (loaded back at a restore) in the same way as the official image | **no bench yet** |
| Every backup after the first one uploads only what has changed | `bench:backup_quarter_hourly#stage-6-only-the-change-is-uploaded` |
| Every backup holds the monitoring history of Gatus, Healthchecks and Beszel | `bench:backup_quarter_hourly#stage-6-only-the-change-is-uploaded` |
| Before each backup, the databases of Gatus, Healthchecks and Beszel are copied whole while they run, and a failed copy fails the backup | `bench:ce_restore#stage-2a-the-monitoring-databases-are-copied-whole` |
| The copy of the Gatus, Healthchecks and Beszel databases taken before each snapshot follows no symbolic link in the monitoring volumes and runs with no privilege beyond reading them; a symbolic link planted there fails the backup | **no bench yet** |
| The backup repository compresses the data it stores | **no bench yet** |
| A backup waits for each deployed database to answer; a database that never answers is left out, everything else is backed up, and the backup fails and names the application | `bench:backup_quarter_hourly#stage-9-the-backup-waits-for-its-databases` |
| A backup waits up to ten minutes for each deployed database to answer | **no bench yet** |
| Once a day, the first backup after midnight in the server's time zone removes the backups beyond what the retention numbers keep, and checks the repository | `bench:backup_quarter_hourly#stage-8-the-days-first-run-cleans-up` |
| A backup stays listed and can be restored until the clean-up removes it under the retention numbers | **no bench yet** |
| After the backup destination changes, the System page says "Integrity verified" only once the integrity check has passed on the new destination | `bench:backup_destination_creates_repository#stage-3b-integrity-waits-for-a-check-of-the-new-repository` |
| A restore runs on the server and is started from the restore page | `bench:ce_restore#stage-4-ce-restore`, `bench:inplace_restore_suite#wizard-restore-stage-4-restore-through-the-page` |
| The restore page is shown in English or French, following the browser's language, and offers the same actions in both languages | `bench:recovery_landing_page_bilingual_parity#stage-1-en-default`, `bench:recovery_landing_page_bilingual_parity#stage-2-fr-variant`, `bench:recovery_landing_page_bilingual_parity#stage-3-action-target-parity` |
| A restore of a snapshot that the repository does not hold is refused before it changes anything | `bench:fi_b3_snapshot_id_mismatch#stage-1-phony-snapshot-rejected`, `bench:fi_b3_snapshot_id_mismatch#stage-3-nothing-held` |
| A restore that needs more disk space than the server has free is refused before anything is stopped, and the refusal names the space that is missing | `bench:fi_d3_postgres_oom_mid_restore#stage-1-f-d3-refuses-restore`, `bench:fi_d3_postgres_oom_mid_restore#stage-2-nothing-held` |
| A restore reads this server's repository, another server's repository or the offsite copy in the same way, with a key that is only allowed to read, and writes nothing to the offsite copy | `bench:worm_round_trip#stage-6b-restore-from-the-offsite-copy`, `bench:dr_suite#stage-13-disaster-recovery`, `bench:ce_restore#stage-4-ce-restore` |
| A restore writes nothing to this server's own repository | **no bench yet** |
| A whole-server restore puts the data back, takes on the settings stored with it, and starts the services Catena runs | `bench:dr_suite#stage-13-disaster-recovery`, `bench:dr_suite#stage-14-post-recovery-assertions`, `bench:dr_suite#stage-19-onbox-store` |
| A whole-server restore starts Gatus, Healthchecks and Beszel on the copies of their databases that the backup took whole | `bench:ce_restore#stage-4a-the-monitoring-databases-come-back-whole` |
| A whole-server restore joins the private network that its settings name | **no bench yet** |
| When a whole-server restore takes on settings whose object storage keys the provider has revoked, the restore reads the repository with the keys entered on the restore page, and backups resume with those keys once they are saved with the destination | `bench:post_compromise_rebuild#stage-3-restore-with-pair-b`, `bench:post_compromise_rebuild#stage-3d-destination-confirmed`, `bench:post_compromise_rebuild#stage-4-store-holds-pair-b`, `bench:post_compromise_rebuild#stage-5-backup-on-pair-b` |
| Restored client applications, other than Gatus, Healthchecks and Beszel, stay stopped until an admin starts them from the restore page, which then replays their databases | `bench:dr_suite#stage-14-applications-held`, `bench:dr_suite#stage-15-applications-started`, `bench:dr_suite#stage-15-database-replay-honest`, `bench:inplace_restore_suite#wizard-restore-stage-5b-client-applications-held`, `bench:inplace_restore_suite#wizard-restore-stage-6b-held-applications-started`, `bench:post_compromise_rebuild#stage-3c-applications-started` |
| An application restored from a backup taken while it was in its backup mode (the pause the application declares so that nothing writes to its database while the backup copies it) comes back as it was at that backup, with its database whole | `bench:mailserver_webmail_backup_mode#stage-6-started-and-whole` |
| A database replay recreates a Postgres role that the dump holds and the database server lost, and gives that role back the objects it owned | `bench:fi_d2_pg_dumpall_replay_constraint#stage-4-replay-recreates-role`, `bench:fi_d2_pg_dumpall_replay_constraint#stage-5-ownership-restored` |
| Starting the restored applications takes each one out of its backup mode, the pause it declares for backups | `bench:inplace_restore_suite#app-restore-stage-4b-backup-mode-released`, `bench:sovereign_exit#stage-2b-held-applications-started-from-a-shell` |
| Starting the restored applications from the restore page runs a converge on the server | **no bench yet** |
| A one-application restore keeps only that application stopped; a migration keeps no application stopped | `bench:inplace_restore_suite#app-restore-stage-4-scope-held`, `bench:inplace_restore_suite#wizard-app-restore-stage-6-scope-held`, `bench:mailserver_webmail_backup_mode#stage-5-restored-and-held`, `bench:wizard_migrate_round_trip#stage-5-data-moved-source-recoverable` |
| A one-application restore brings back, with its data, an application that was removed from the server, from a backup that holds it; the restore page lists the applications that a backup holds and the server no longer runs | **no bench yet** |
| During a migration, the first copy of the bulk of the data starts no application on the new server while the old server keeps serving, and a second copy pass replaces the database archives that the first pass left | `bench:migrate_preseed_no_split_brain#stage-3-nothing-started-here`, `bench:migrate_preseed_no_split_brain#stage-4-second-pass-sweeps-old-archives` |
| A migration that fails after the old server has stopped leaves the old server stopped but reachable, says so and records the failure, never moves the traffic, and never restarts the old server by itself | `bench:wizard_migrate_resume_source#stage-3-no-automatic-resume`, `bench:wizard_migrate_resume_source#stage-5-traffic-never-moved` |
| After a failed migration, one command puts the old server back into service and leaves stopped any application that its admin had stopped | `bench:wizard_migrate_resume_source#stage-4-resume-the-source` |
| While a migration takes its snapshot, the old server's applications are paused and its databases keep answering | `bench:wizard_migrate_resume_source#stage-3-no-automatic-resume` |
| After a whole-server restore, backups wait until an admin confirms the backup destination or saves a new one, and a converge in the meantime succeeds | `bench:dr_suite#stage-14-backups-wait`, `bench:rebuild_backs_up_after_restore#stage-3a-the-backups-wait`, `bench:ce_restore#stage-4.1b-the-backups-wait`, `bench:post_compromise_rebuild#stage-3a-the-backups-wait`, `bench:dr_suite#stage-16-settle-converge` |
| A converge reports the backups that wait after a restore as waiting, never as failed | **no bench yet** |
| On a licensed server, a whole-server restore turns off the updates and the engine upgrade, and every page shows a warning about it | `bench:worm_round_trip#stage-6c-backups-wait` |
| After a whole-server restore on a licensed server, the updates and the engine upgrade stay off until an admin turns them back on | **no bench yet** |
| A new backup destination gets a new backup password | `bench:rebuild_backs_up_after_restore#stage-7-a-new-destination`, `bench:worm_round_trip#stage-7c-fresh-repository` |
| An interrupted restore resumes from the same snapshot, and refuses to restore more than it was first asked to | `bench:fi_n5_provider_outage_mid_restore#stage-2-resume-restore-succeeds`, `bench:inplace_restore_suite#app-restore-stage-6-halted-scope-refuses-to-widen` |
| A restore leaves out a database export whose application or database service the backup does not hold, and reports it; a restore fails when the database service of an export that the backup holds does not start; and removing an application removes its database exports | `bench:ce_restore#stage-4.1a-the-export-of-a-removed-application-is-reported`, `bench:fi_n5_provider_outage_mid_restore#stage-2-resume-restore-succeeds` |
| An interrupted restore refuses a different snapshot, is never reported as finished, and only a run that finishes clears its record | **no bench yet** |
| The offsite copy copies the repository's objects into a locked bucket at another provider, and refuses a locked bucket that holds a different repository | `bench:worm_round_trip#stage-1-mirror-to-worm`, `bench:offsite_copy_unreachable_target#stage-7-another-repository-refused` |
| The offsite copy reads the repository with source keys of its own: once the backup's old keys are revoked, it copies nothing until its source keys are changed in the dashboard | `bench:s3_backup_keys_rotation_offsite_copy#stage-2-copy-runs-on-pair-a`, `bench:s3_backup_keys_rotation_offsite_copy#stage-4-copy-refused-with-pair-a`, `bench:s3_backup_keys_rotation_offsite_copy#stage-5-copy-follows-pair-b` |
| Each offsite copy first removes the backups beyond what the retention numbers keep, and while an offsite copy is set up, that removal only deletes stored files and never rewrites them | `bench:worm_round_trip#stage-0b-dropped-backups-stay-out-of-the-copy`, `bench:rclone_copy_preserves_pruned_packs#stage-5-cold-still-has-pruned-snaps` |
| Restoring a bucket from the restore page copies its objects from the locked bucket into the original bucket or into a new empty bucket, and the offsite copy then copies from the bucket that received them | `bench:worm_round_trip#stage-7a-a-bucket-put-back` |
| The restore page lists the restic repositories that a locked bucket holds | `bench:worm_round_trip#stage-7c-fresh-repository` |
| A backup repository recovered into a new bucket keeps receiving backups, and its offsite copy carries on in the same place in the locked bucket; the offsite copy of a new repository goes to a new folder in that locked bucket | `bench:worm_round_trip#stage-6c-backup-repository-continues`, `bench:worm_round_trip#stage-7c-fresh-repository` |
| A backup repository is never recovered into the bucket it was copied from, and its offsite copy continues only once the locked bucket proves to hold the same repository | **no bench yet** |
| A restore from a backup brings back the content the backup held, and the applications with it | `bench:backup_rollback`, `bench:ce_restore` |
| An application keeps data in a folder outside its volumes only once an admin lists that folder in Settings; the folder is then backed up, also when it sits on another disk, and a restore brings it back | `bench:app_extra_disk_round_trip` |
| An application keeps its data in a folder inside a folder listed in Settings, never in the listed folder itself | **no bench yet** |
| A one-application restore brings back the application's data kept under a folder listed in Settings, and nothing else from that folder; the restore refuses before touching anything when the server no longer lists the folder | `bench:app_extra_disk_round_trip#stage-11-one-application-restore` |
| While a folder listed in Settings that was on a mounted disk at the last backup is not mounted, a backup stops before any application enters its backup mode, a restore refuses before touching anything, and the applications that use that disk are not deployed | `bench:app_extra_disk_round_trip#stage-12-unmounted-disk-refused` |
| A restore refuses before touching anything when a disk it writes to lacks room for the data it puts back | **no bench yet** |
| An application whose disk is not mounted when the server starts stays stopped, and comes back at the next start of the server, or when it is deployed, once the disk is mounted again | `bench:app_extra_disk_round_trip#stage-13-held-at-start-up`, `bench:app_extra_disk_round_trip#stage-14-back-once-mounted` |
| The backup repository's password can be replaced without losing any backup | `bench:restic_password_rotation_round_trip` |
| With the dashboard deleted, backups still run, restores still work and the applications stay online | `bench:sovereign_exit`, `bench:recovery_readme_manual_restore` |

### The timers -- `public_ports`, `infrastructure`, `host_maintenance`

A scheduled job is one task that runs on a timer: the backup, the bit-rot check,
the restore test, the offsite copy, the maintenance (which backs up, checks and
updates), the dashboard updates, the scheduled converge (a run that brings the
server back to what Catena declares), the external heartbeat or the
vulnerability watch. Every scheduled job ships with the host engines, the
programs Catena takes from the dashboard image, and every job except the
vulnerability watch ships with its timer turned off. `catena-schedule apply`
turns a job's timer on, at the schedule set in the dashboard, when the server's
edition covers that job. Each job takes the locks of what it changes: a job that
changes the whole server runs alone, while jobs that change different
applications, or one application and the backup repository, run at the same
time. Debian's unattended-upgrades applies the operating system packages.

| Invariant | Enforced by |
| --- | --- |
| This repository ships no timer, and every scheduled job ships with the host engines | `audit:check-port`, `threat:CV8` |
| After a converge, the vulnerability watch is the only scheduled job a server has turned on | `bench:backup_schedule_applied#stage-1-only-the-watch-is-scheduled-by-default`, `bench:community_free_schedules#stage-1-only-the-watch-is-enabled` |
| A converge turns on the timers of the public-port reconciler (which applies the ports each role declares to the firewall), of the dashboard sync and the Gatus sync, of the hourly checks of whether the server needs a restart, of the licence and of each application, and of the error reports every five minutes; none of these timers uses object storage | **no bench yet** |
| The System page lists by name each scheduled job it counts, with the job's state, and each job's name links to the page where that job is set up or where its results are shown | `bench:community_security_alerts#stage-3c-each-counted-job-is-named` |
| A scheduled job turns on only on a server whose edition covers that job, and runs at the schedule saved in the dashboard | `bench:backup_schedule_applied#stage-3-enable-applies`, `bench:community_free_schedules#stage-2-every-other-lane-refused` |
| A server with no paid edition schedules only the backup, no more often than once a week, and the vulnerability watch, no more often than once a day, and refuses a more frequent schedule | `bench:community_free_schedules` |
| On a server with no paid edition, the Schedules page shows each job that a paid edition runs with a gray Pro or Business badge, and that job's controls are turned off | `bench:community_free_schedules#stage-2b-page-says-what-is-free-and-what-runs-by-hand` |
| A server that loses its paid edition keeps running its scheduled backup and its vulnerability watch, at their default times when the saved schedule runs them more often than a server with no paid edition allows | `bench:ee_lapse#stage-3-host-lapsed`, `bench:community_free_schedules#stage-6-a-faster-saved-backup-runs-weekly` |
| A server that loses its paid edition turns off the timer of every scheduled job except the backup and the vulnerability watch, and keeps the schedules that were saved | **no bench yet** |
| Every server can run a backup by hand, whatever schedule the backup has | `bench:ce_backup_deferred#stage-2-backup-now-overrides` |
| Every server can run the updates, a converge and a restart by hand | **no bench yet** |
| A dashboard update applies the configuration that comes with the new dashboard image by running a converge, and puts the previous dashboard back when that converge fails | `bench:catena_admin_self_update#stage-2-self-update`, `bench:catena_admin_self_update#stage-5-failed-converge-puts-it-back` |
| A Catena release moves each service Catena runs, with its data, to the version the release ships, and when the update to the release fails, or the server's health drops after the converge, the previous versions come back with their data | `bench:release_moves_dependencies#stage-4-every-service-on-the-release`, `bench:release_moves_dependencies#stage-6-the-failed-release-is-rolled-back`, `bench:release_moves_dependencies#stage-7-a-regression-after-the-converge` |
| The scheduled backup starts at its set time, with no random delay, and two backup runs never overlap | `bench:backup_quarter_hourly#stage-3-the-lane-starts-on-the-quarter-hour`, `bench:backup_quarter_hourly#stage-5-the-cadence-holds` |
| Every timer starts at its set time with no random delay, except the licence check, which starts at a minute chosen for each server and fixed for that server | **no bench yet** |
| A scheduled job that is still running when its next set time comes is not started a second time, and the job next starts at the first set time after that run ends | **no bench yet** |
| The checks each scheduled job has in the server's own Healthchecks follow that job's schedule, and the checks of a job that is turned off, or that the server's edition does not cover, are paused | `bench:backup_schedule_applied#stage-3b-checks-follow-the-lane`, `bench:backup_schedule_applied#stage-4b-checks-paused-with-the-lane`, `bench:community_free_schedules#stage-4-lane-checks-follow-the-lanes` |
| The external heartbeat ships turned off; while it is on, it calls the outside monitoring URL saved in the dashboard and names any monitor that stopped answering, and it stops calling when it is turned off | `bench:fi_o5_deadman_off_by_default#stage-1-heartbeat-shipped-and-off`, `bench:external_heartbeat_round_trip` |
| The external heartbeat runs only while the server has an active paid licence | **no bench yet** |
| Each application that declares a backup mode (a state in which its data holds still for the backup) stays in that mode for the whole backup, whether the backup runs by hand, on its schedule or in the maintenance, and comes out of it once the backup ends, whether the backup succeeded, failed or was stopped | `bench:backup_mode_on_every_backup#stage-1-backup-now-uses-the-backup-mode`, `bench:backup_mode_on_every_backup#stage-2-scheduled-backup-uses-the-backup-mode`, `bench:backup_mode_on_every_backup#stage-3-maintenance-uses-the-backup-mode`, `bench:backup_mode_on_every_backup#stage-4-a-stopped-backup-releases-the-apps`, `bench:daily_chain_quiesce_invoked#stage-2-backup-mode-around-the-backup`, `bench:daily_chain_quiesce_invoked#stage-3-backup-fails` |
| An application kept in its backup mode for several minutes keeps its containers running, and a sign-in started while the application is in that mode completes once the application comes out of it | `bench:mailserver_webmail_backup_mode#stage-3-night-with-the-hold`, `bench:mailserver_webmail_backup_mode#stage-4-answers-after-the-release` |
| When a backup is killed while applications are in their backup mode, the applications stay in that mode until the server starts again, and that start takes them out of it; a maintenance run killed during its backup takes the backup again when the run resumes | `bench:backup_mode_on_every_backup#stage-5-a-killed-backup-is-released-at-boot`, `bench:backup_mode_on_every_backup#stage-6-an-interrupted-maintenance-resumes` |
| The commands that put an application into its backup mode and take it out run inside the application's own container | `bench:app_declarations_travel_with_the_app#stage-3-backup-mode-with-the-catalog-gone`, `bench:app_declarations_travel_with_the_app#stage-6-hand-deployed-stack-gets-its-backup-mode` |
| No command of an application's backup mode runs outside that application's container | **no bench yet** |
| When a backup-mode command runs past its time limit but ends within its grace period, the command has finished before the next command starts | `bench:app_declarations_travel_with_the_app#stage-6b-a-backup-mode-past-its-time-ends-first` |
| An application's command that is still running at the end of its grace period is stopped before the next command starts, the grace period lasts as long as the command's time limit and two minutes at most, and the maintenance log names the stopped command along with any application left in its backup mode | **no bench yet** |
| An update stops each of an application's commands at its time limit: when a command that runs before the update is stopped, the update does not go ahead, and when a migration is stopped, the update is rolled back | **no bench yet** |
| The maintenance applies updates on a server that has no backup configured, and the maintenance log says that no backup was taken before those updates | `bench:daily_chain_updates_without_backup#stage-1-no-backup-updates-run` |
| While no backup is configured, every page of the dashboard shows a warning | **no bench yet** |
| While backups are paused after a restore, the maintenance still applies its updates | **no bench yet** |
| The maintenance runs its steps in order to the end, clears the state it saved for resuming, and records the run as done | `bench:daily_chain_full_pass#stage-3-chain-runs-to-done`, `bench:daily_chain_full_pass#stage-4-history-entry`, `bench:daily_chain_full_pass#stage-5-status-idle-after-done`, `bench:ee_daily_cycle#stage-3-run-chain`, `bench:ee_daily_cycle#stage-4-status-history` |
| A failed container update does not stop the maintenance: the later steps still run, and the run is recorded as done | `bench:daily_chain_container_rollback#stage-2-chain-reaches-done-despite-container-fail`, `bench:daily_chain_container_rollback#stage-3-state-cleared-on-done` |
| A maintenance run with too little free disk space stops at its backup step, before any update, and records that the disk space is low | `bench:daily_chain_preflight_aborts_low_disk#stage-2-chain-aborts-at-the-backup`, `bench:daily_chain_preflight_aborts_low_disk#stage-3-state-preserved`, `bench:daily_chain_preflight_aborts_low_disk#stage-4-report-records-disk-low` |
| While the maintenance is turned on, a maintenance run cut short by a restart of the server resumes at boot from the state it saved | `bench:daily_chain_resumes_after_reboot#stage-1-lane-on-enables-resume-at-boot`, `bench:daily_chain_resumes_after_reboot#stage-4-chain-resumed-to-done`, `bench:daily_chain_resumes_after_reboot#stage-5-lane-off-unlinks-resume` |
| When the state the maintenance saved is damaged, the next maintenance run starts again from the first step, and the log shows what was found | `bench:daily_state_corrupt_fallback#stage-2-corrupted-state-starts-new-night` |
| The maintenance stops before any update while a service the monitoring checks is failing, and names the failing service | `bench:ee_daily_cycle#stage-5-gate-stops-on-a-red-endpoint` |
| On a server where nobody has chosen the maintenance's steps, the maintenance runs every step except the vulnerability watch and the restore test; once the steps are chosen, the maintenance runs only the steps that are turned on | `bench:daily_chain_step_list#stage-1-defaults-rendered-en-fr`, `bench:daily_chain_step_list#stage-3-night-runs-only-the-chosen-steps` |
| The restore test and the vulnerability watch each run either on their own schedule or as a step of the maintenance, never both | `bench:daily_chain_step_list#stage-9b-restore-test-step-or-lane`, `bench:daily_chain_step_list#stage-9-cve-watch-step-or-lane` |
| The restore test restores the latest backup into a scratch area and checks the result | **no bench yet** |
| The restore test starts the restored databases and each application that declares a start check, and checks that each of them opens | `bench:verify_hot_bootprobe_weekly#stage-3-forced-bootprobe-runs` |
| The restore test starts the restored databases and applications on a network of their own, with no port published | **no bench yet** |
| The offsite copy runs only after the backups past their retention have been removed and the repository check has passed, and, when the option is turned on, only after a restore test of the newest backup has passed | `bench:offsite_copy_refuses_an_unchecked_backup#stage-2-repository-check-refuses`, `bench:offsite_copy_refuses_an_unchecked_backup#stage-4-option-on-refusals`, `bench:worm_round_trip#stage-0b-dropped-backups-stay-out-of-the-copy` |
| The backup, its repository check, the restore test or the health check that runs before the updates can each be left out of the maintenance, with a warning, and when the updates run without the backup step, every page of the dashboard shows a warning | `bench:daily_chain_step_list#stage-5-updates-without-backup-warn`, `bench:daily_chain_step_list#stage-6-health-gate-left-out-warns`, `bench:daily_chain_step_list#stage-8-backup-without-its-check-warns`, `bench:daily_chain_step_list#stage-9a-untested-backup-means-no-restore-test-scheduled` |
| The offsite check can be left out of the maintenance, with a warning | **no bench yet** |
| A failed offsite check stops the maintenance before any update when the server is set to require a good offsite copy before updates, and lets the updates run when the server is not | `bench:daily_chain_verify_cold_fail_configurable#stage-2-require-true-aborts`, `bench:daily_chain_verify_cold_fail_configurable#stage-3-state-preserved-on-abort`, `bench:daily_chain_verify_cold_fail_configurable#stage-4-require-false-continues`, `bench:daily_chain_verify_cold_blocks_mirror#stage-2-chain-aborts-on-cold-fail` |
| While the last offsite check has failed, the offsite copy of the backup repository refuses to run, a copy of objects from any other bucket still runs, and the offsite copy of the backup repository runs again once the check passes | `bench:daily_chain_verify_cold_blocks_mirror#stage-3-sentinel-written-fail`, `bench:daily_chain_verify_cold_blocks_mirror#stage-4-repository-copy-refuses`, `bench:daily_chain_verify_cold_blocks_mirror#stage-5-unrelated-copy-not-gated`, `bench:daily_chain_verify_cold_blocks_mirror#stage-6-sentinel-cleared-on-recovery` |
| A failed restore test or a failed repository check stops the maintenance before any update | `bench:daily_chain_verify_hot_fail_aborts_updates#stage-4-update-tail-skipped`, `bench:restic_check_subset_weekly#stage-8-night-stops-before-the-updates` |
| When the backup fails, the maintenance run ends at that point and is recorded as failed at the backup step | `bench:daily_chain_quiesce_invoked#stage-3-backup-fails` |
| A failed backup stops the maintenance before any update, unless the backup step is left out of the maintenance | **no bench yet** |
| The engine upgrade moves the container engine to the version this repository pins, and puts the previous engine back when the new one does not come up | `bench:docker_engine_upgrade_round_trip#stage-2-a-new-engine-that-does-not-start-goes-back`, `bench:docker_engine_upgrade_round_trip#stage-3-the-new-engine-holds` |
| Updating Catena from the dashboard moves Docker to the version the new release pins | `bench:update_from_previous_release#stage-3a-the-engine-runs-the-release-pin` |
| In the maintenance, the container engine upgrade runs after the container updates | `bench:ee_daily_cycle#stage-3-run-chain` |
| In the maintenance, the container engine upgrade stays within the major version the engine already runs | **no bench yet** |
| The maintenance's last step restarts the server when an update needs a restart, also after a run that stopped before changing anything, and checks that every service came back | `bench:daily_chain_reboot_round_trip#stage-3-the-chain-restarts-the-host`, `bench:daily_chain_reboot_round_trip#stage-4-everything-came-back`, `bench:daily_chain_step_list#stage-14-stopped-night-still-restarts` |
| The maintenance reports to its own alert check and records its result, or the step that failed, in the maintenance log | `bench:daily_chain_step_list#stage-3-night-runs-only-the-chosen-steps`, `bench:daily_chain_step_list#stage-10-failed-step-recorded`, `bench:daily_umbrella_healthchecks#stage-1-success-path-pings-start-and-succeeded`, `bench:daily_umbrella_healthchecks#stage-2-abort-path-pings-fail`, `bench:nightly_update_real#stage-3-after-the-night` |
| A job that changes the whole server waits for every other job to end and then runs alone, and jobs started after it wait for it to finish; jobs that change different applications, or one application and the backup repository, run at the same time; a change of the backup password made in the dashboard while a whole-server job runs gets the answer that the server is busy | `bench:concurrent_backup_lock_contention#stage-1-units-take-the-host-lock`, `bench:concurrent_backup_lock_contention#stage-5-settings-answers-busy`, `bench:concurrent_backup_lock_contention#stage-6-a-repository-check-runs-beside-an-application-start`, `bench:concurrent_backup_lock_contention#stage-7-a-configuration-run-waits-for-every-scoped-lane`, `bench:concurrent_backup_lock_contention#stage-8-a-waiting-whole-server-lane-goes-first`, `bench:daily_chain_step_list#stage-13-night-waits-then-holds-the-lock`, `bench:ee_daily_cycle#stage-6-night-lends-the-host-lock`, `bench:apps_folder_round_trip#stage-17-two-applications-change-side-by-side`, `bench:apps_folder_round_trip#stage-18-a-server-wide-task-waits-for-both` |
| A maintenance run that cannot take the server-wide lock in time is skipped and reported, and the maintenance runs again at its next scheduled time | `bench:daily_chain_step_list#stage-12-night-skipped-while-the-lock-is-held` |
| A maintenance run waits up to an hour for the server-wide lock | **no bench yet** |
| The vulnerability watch runs on every server, including one with no licence, against a freshly downloaded vulnerability database and the list of vulnerabilities known to be exploited | `bench:community_security_alerts#stage-2-the-watch-runs-with-no-licence`, `bench:cve_watch_emergency_patch#stage-3-database-downloaded-into-place` |
| On every edition, a vulnerability known to be exploited raises one alert, whether or not a fixed version exists, and the alert names each affected application, its image, the installed version of the package and the version that fixes it | `bench:community_security_alerts#stage-3-a-fixable-vulnerability-alerts-once`, `bench:community_security_alerts#stage-4-an-exploited-one-with-no-fix-alerts-once`, `bench:community_security_alerts#stage-5-no-second-alert` |
| The maintenance log records each vulnerability alert with the application, its image and the version that fixes the vulnerability | `bench:community_security_alerts#stage-3-a-fixable-vulnerability-alerts-once` |
| A vulnerability alert is not counted as a failed job or as a service that is down | `bench:community_security_alerts#stage-3b-an-alert-is-no-failed-job` |
| The vulnerability watch raises an alert for a vulnerability that attackers are not known to exploit only when the vulnerability is critical or high and an update offered on this server fixes it | `bench:community_security_alerts#stage-7-every-finding-follows-the-rule`, `bench:community_security_alerts#stage-7b-a-fix-only-in-its-package-does-not-alert` |
| The vulnerability watch records every critical, high and medium vulnerability, and a medium one raises an alert only when attackers are known to exploit it | **no bench yet** |
| The vulnerability scan writes a well-formed report, even when it finds nothing | `bench:cve_residual_emits_findings#stage-2-cve-scan-shape`, `bench:cve_residual_emits_findings#stage-4-rerun-preserves-shape` |
| On every edition, an application's card says when the version the application runs has a vulnerability the watch alerts on, whether attackers exploit that vulnerability and whether an update fixes it, and the application's page lists every vulnerability of that version that attackers exploit or that an offered update fixes | `bench:community_security_alerts#stage-8b-the-card-and-the-app-page-name-it` |
| Without Catena Business, the Vulnerabilities page and each application's page list every vulnerability that attackers exploit and every one that an offered update fixes, each with its package, installed version and fixed version, and count the other vulnerabilities by severity | `bench:community_security_alerts#stage-8-the-findings-to-act-on-show-and-the-rest-are-counted` |
| With Catena Business, the Vulnerabilities page and each application's page list every vulnerability the watch found | `bench:community_security_alerts#stage-8c-business-lists-every-finding` |
| The software inventory, and the download of each image's software bill of materials, need Catena Business | `bench:cve_watch_emergency_patch#stage-10b-pro-withholds-the-business-tools`, `bench:activate_ee#stage-3-catena-business` |
| On every edition, an administrator can run the vulnerability watch on demand and confirm an emergency patch, and an emergency patch accepts only a vulnerability that the Vulnerabilities page lists | `bench:community_security_alerts#stage-8d-a-person-patches-on-community`, `bench:cve_watch_emergency_patch#stage-16-lapsed-licence-keeps-the-hand-patch`, `bench:cve_watch_emergency_patch#stage-10b-pro-withholds-the-business-tools` |
| Applying emergency patches automatically needs a paid edition | `bench:cve_watch_emergency_patch#stage-16-lapsed-licence-keeps-the-hand-patch` |
| An emergency patch moves a service to the oldest release that the service's update policy allows and that a scan on the server shows to be free of the vulnerability, and rolls the service back when its health drops | `bench:cve_watch_emergency_patch#stage-13-patched-to-oldest-free-release`, `bench:cve_watch_emergency_patch#stage-12-regression-rolls-back`, `threat:CV16` |
| Automatic emergency patches fix only a vulnerability known to be exploited, never move a service kept off the updates, and never run while updates are paused | `bench:cve_watch_emergency_patch#stage-14-automatic-moves-only-what-it-may` |
| An emergency patch is protected by the same copies an update takes: the database snapshot and the volume copies made before the deploy | **no bench yet** |
| unattended-upgrades applies operating system packages on every server, licensed or not, from Debian's security updates, stable updates and point releases, and from the private-network client's own package repository | `bench:ce_validate#stage-1-ce-validate`, `bench:reconcile_repairs_host_upkeep#stage-3-restored` |
| A service still running on a library that an update replaced is restarted after the update, while the container engine is left until the server restarts | `bench:reboot_required_notified#stage-6-replaced-libraries` |
| A server that needs a restart shows that on the dashboard until the restart happens, and without a licence the server waits for a person to restart it | `bench:reboot_required_notified#stage-3-pending-reboot-reported`, `bench:reboot_required_notified#stage-5-host-was-not-rebooted` |

### The release manifest -- written last, by both converge paths

Every converge, whether it runs from `converge.yml` or from `reconcile.yml`,
removes the files it no longer ships and then records what it delivered.
`/var/lib/catena/payload-manifest.json` lists the files the host engines claim.

| Invariant | Enforced by |
| --- | --- |
| Every converge records what it delivered | `bench:converge_suite#stage-1-manifest-describes-this-converge`, `bench:converge_suite#stage-7a-the-action-is-back`, `bench:reconcile_is_a_noop#stage-1-first-reconcile` |
| A converge removes a file that an earlier converge installed and that the current converge no longer ships, unless the file was changed after it was installed, in which case the file stays | `bench:install_rerun#stage-4b-withdrawn-pruned-drift-reported`, `bench:payload_prune_respects_ce#stage-2-withdrawn-removed` |
| A converge never removes a Community file as though it were a licensed file, and never removes a file the host engines claim | `bench:payload_prune_respects_ce#stage-1-coexistence`, `bench:payload_prune_respects_ce#stage-3-foreign-copy-survives`, `workflow:ci.yml#installer` |

### The version stamp -- written first, by both converge paths

`/etc/catena/version.txt` names the version of this repository that a converge
applied, in the form `git describe` prints. The converge takes that version from
the record of origin the release image keeps beside its tree. Every backup holds
this file.

| Invariant | Enforced by |
| --- | --- |
| `converge.yml` and `reconcile.yml` each write in the version stamp the version of the image whose tree they ran | `bench:reconcile_is_a_noop#stage-1-first-reconcile`, `bench:update_from_previous_release#stage-3-one-version-after-the-update` |
| Every snapshot names the version of Catena that produced its data | `bench:recovery_readme_manual_restore#stage-2-readable-from-snapshot` |
| The version stamp is the first thing a converge writes, and a converge from a tree with no record of its origin writes no stamp and leaves the previous one in place | **no bench yet** |
| A restore refuses a snapshot taken on a newer version before it changes anything, and restores a snapshot taken on an older version | `bench:inplace_restore_suite#skew-abort-stage-2-restore-refuses`, `bench:inplace_restore_suite#skew-abort-stage-3-zero-side-effects`, `bench:inplace_restore_suite#skew-upgrade-stage-2-an-older-snapshot-passes-the-gate`, `bench:fi_n5_provider_outage_mid_restore` |
| A restore refuses a backup whose version stamp cannot be compared with the server's version | **no bench yet** |
| A restore of a snapshot taken on an older version runs each restored application's declared migration (the commands that bring its data up to the new version) once the application's data is back, never stops a migration part-way, waits at least half an hour for it, and reports any migration still running after that | **no bench yet** |
| A migration from one server to another is refused unless both servers run the same version | **no bench yet** |

## Applications

The dashboard deploys applications from a catalogue that each server resolves
for itself. Each application lives in its own folder,
`/srv/catena/compose/<stack>`, which holds its compose file, its `.env` and a
record of where it came from, and the server's `catena-apps` command deploys
it on Docker Swarm. The `x-catena` block of its compose file declares its
settings and its commands. The compose files of the catalogue applications and
their wiring live in `catena-templates`. Catena's shared services answer at the
subdomains `auth`, `dash`, `gatus`, `healthchecks` and `beszel` of the server's
domain. A converge is a run that brings the server back to what Catena
declares. A sign-in gate stands in front of a URL and sends a visitor to sign
in before the visitor reaches it. The sign-in realm is the set of accounts and
settings the sign-on service keeps for the people who use the server. Each
application that uses single sign-on is registered in that realm, and its
registration admits one group of accounts, administrators or staff, or every
account when the labels in the application's compose file open it to clients;
the registration also admits the members of the access groups the application
declares. Catena's sign-on extension
(`ansible/reconcile/roles/keycloak/provider`) refuses everybody else.

| Invariant | Enforced by |
| --- | --- |
| Each catalogue application is routed on its own URL | `bench:catalog_apps_read_green_in_gatus#stage-1-routed` |
| An application can publish several URLs, and each URL gets its own route, its own status check and its own sign-in gate; a URL that another application holds, or one of Catena's own, is refused, and Traefik labels in an application's compose file route nothing | `bench:app_several_addresses#stage-1-a-route-per-address`, `bench:app_several_addresses#stage-3-access-per-address`, `bench:app_several_addresses#stage-4-a-cookie-opens-its-own-address-alone`, `bench:app_several_addresses#stage-5-a-status-card-per-address`, `bench:app_several_addresses#stage-6-a-taken-host-is-refused`, `bench:app_several_addresses#stage-9-catena-addresses-refused`, `bench:app_several_addresses#stage-10-traefik-labels-route-nothing` |
| A URL behind a sign-in gate can name exact paths that are open to everyone, and every other path of that URL still sends a visitor to sign in | `bench:app_several_addresses#stage-12-public-paths-skip-the-gate` |
| Access to an application's URL can be set by one of the application's settings (open to everyone, to anyone who is signed in, or to administrators only), and a saved change of that setting takes effect the next time the application is deployed | `bench:app_several_addresses#stage-13-access-read-from-the-settings` |
| When the setting that controls access to an application's URL holds a value other than public, private or admin-only, only administrators, or the groups the application names, can open that URL | `bench:app_several_addresses#stage-13-access-read-from-the-settings` |
| An application that asks for single sign-on gets its own registration in the sign-on service, which follows the labels in the application's compose file and is removed with the application; registrations made by hand are left alone | `bench:oidc_label_client_provisioned`, `bench:mailserver_after_domain#stage-3-wired-without-a-press`, `threat:CV13` |
| When an application's registration with the sign-on service is deleted while the application stays deployed, Catena registers it again at its next sync with the secret kept in the application's settings file, without redeploying the application | `bench:oidc_label_client_provisioned#stage-5-lost-entry-made-again-with-its-folders-secret` |
| Catalogue applications that offer single sign-on in their free edition sign people in with their Catena account | `bench:nextcloud_sign_in` |
| An account that is neither in the group an application's registration admits nor in one of the application's access groups is refused on the sign-in page, also when it signs in through an external identity provider, and a refusal because of the account's group does not count as a failed sign-in | `bench:catalog_app_sign_in#stage-client-tier-refused`, `bench:catalog_app_sign_in#stage-broker-tier-check`, `bench:catalog_app_sign_in#stage-access-refused-without-it`, `bench:nextcloud_sign_in#stage-access-refused-without-it`, `workflow:ci.yml#keycloak-provider`, `threat:CV2` |
| Every external identity provider, including one added for an organization, checks that the account is in a group the application admits from the moment the provider is added, and a converge leaves the sign-in flows as they are | `bench:catalog_app_sign_in#stage-broker-before-converge`, `bench:catalog_app_sign_in#stage-broker-organization-provider`, `bench:catalog_app_sign_in#stage-broker-converge-keeps-the-flows`, `workflow:ci.yml#keycloak-provider` |
| While outgoing mail is set up, a client can sign in with a code sent to the email address they type, in place of their password; every email address gets the same answer, staff, administrator, disabled and unknown accounts are sent no code and cannot use one, a code that fails to sign in counts toward the lockout, and the code never creates an account | `bench:catalog_app_sign_in#stage-client-email-code`, `workflow:ci.yml#keycloak-provider` |
| Giving a client access to an application lets them sign in to it and gives them the application's role before their first sign-in; removing that access refuses their next sign-in and takes the role back | `bench:catalog_app_sign_in#stage-access-given-signs-in`, `bench:catalog_app_sign_in#stage-access-removed-refused`, `bench:nextcloud_sign_in#stage-access-given-signs-in`, `bench:nextcloud_sign_in#stage-access-removed-refused` |
| A changed sign-in flow is built in full under a new name before the sign-in realm switches to it, so a failed converge leaves sign-in working on the previous version of the flow; a flow edited by hand is replaced by a new version, and versions that are no longer in use are removed | `workflow:ci.yml#installer` |
| The configuration run and the dashboard keep managing the sign-on service on releases of it that accept admin calls only from accounts given a separate admin permission, because the sign-on service gives Catena's own accounts that permission each time it starts | `bench:inplace_restore_suite#held-after-restore-stage-5-through-a-converge`, `bench:keycloak_master_realm_hardening#stage-6-converge-through-the-automation-client`, `bench:identity_group_gates_access`, `workflow:ci.yml#keycloak-provider` |
| Each application gets a password generated on the server | **no bench yet** |
| An application's hooks, the setup commands its `x-catena` block declares, configure it after every deploy, and a further deploy changes nothing | `bench:suite_wiring_commands#stage-talk-hpb-wired`, `bench:app_takes_the_outgoing_mail#stage-4-a-deploy-changes-nothing`, `bench:mailserver_after_domain#stage-3-wired-without-a-press` |
| A certificate an application declares is issued by the server into the application's volume and loaded by the application, and the scheduled renewal replaces a certificate that is due and has the application serve the new one | `bench:fi_c7_coturn_cert_expired#stage-1-renewal-job-renews`, `bench:mailserver_after_domain#stage-3-wired-without-a-press`, `bench:app_declared_needs#stage-1-needs-met-before-the-deploy` |
| An application's hooks, the setup commands its `x-catena` block declares, configure it after every update and every restore | **no bench yet** |
| An application that reads a value another application provides is deployed again with that value once the providing application starts | `bench:host_network_app_ports#stage-5-reader-redeployed` |
| The antivirus and office editing services reach the applications that declare a need for them | **no bench yet** |
| An application deploys from its folder, which holds its compose file, its `.env` and the record of where it came from | `bench:apps_folder_round_trip#stage-1-private-repository-needs-its-token` |
| Each application's folder is owned by root and readable by root only | **no bench yet** |
| An application setting reaches the application exactly as it was typed, `$(...)` and backticks included, and runs nothing | `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed` |
| An application setting that holds a line break is refused, and the setting keeps its previous value | `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed` |
| `catena-apps` runs `docker stack deploy` as root with the `.env` values as its only environment, and refuses the `.env` keys that change how the Docker client or the program loader behaves (`DOCKER_*`, `PATH`, `HOME`, `LD_*`, the proxy variables) | **no bench yet** |
| The app checker, which checks an application's compose file, runs before every deploy, and a compose file it rejects is not deployed | `bench:app_checker_catalog#stage-4-deployed-file-counted-on-its-tile`, `bench:host_network_app_ports#stage-6-hand-copy-refused` |
| A compose file that fails to load is rejected before it reaches a server | `bench:fi_u1_compose_lint_reject` |
| An application whose compose file was edited into a broken state is repaired in place from the source it was published from | `bench:repair_broken_template_round_trip` |
| An application gets the host network, the server's own network with no container network in between, only when its compose file comes from Catena's catalogue and declares it; an application whose catalogue file was changed by hand gets the host network when its edited file declares it, and the application's page and card then say that it runs with the host network | `bench:host_network_app_ports#stage-2-ports-open`, `bench:host_network_app_ports#stage-6-hand-copy-refused` |
| An application gets the container engine's socket, which controls every container on the server, only when its compose file comes from Catena's catalogue and declares it; an application whose catalogue file was changed by hand gets the socket when its edited file declares it, and the application's page and card then say that it runs with the container engine's socket | **no bench yet** |
| Each backup holds the application folders, and a restore brings them back | `bench:app_declarations_travel_with_the_app#stage-5-restore-brings-the-block-back`, `bench:inplace_restore_suite#app-restore-stage-5-definition-rolled-back` |
| An application keeps the way it is backed up, its update steps and its restore steps as they were when it was deployed, so the server needs no catalogue to back it up, update it or restore it | `bench:app_declarations_travel_with_the_app` |
| An application is added from one of the repositories on the server's list of application repositories; a private repository needs its access token, and that token is never shown | `bench:app_origin_update#stage-1-added-on-release-one`, `bench:apps_folder_round_trip#stage-1-private-repository-needs-its-token` |
| An application added from a repository stays stopped, with its settings and generated values written, until a person starts it, and the settings saved before its first start are used in its first deploy | `bench:apps_folder_round_trip#stage-1-private-repository-needs-its-token`, `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed` |
| The server's list of application repositories holds Catena's catalogue until an admin changes it | **no bench yet** |
| The server fetches a repository with git, as the user `catena-git` and over https only; root then reads what was fetched as data only, refuses symbolic links, folders outside the application's folder and folders that are too large, and keeps the access token where only root can read it | **no bench yet** |
| An application's settings come from values a person gives and from values the server already knows | `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed`, `bench:app_declared_needs#stage-1-needs-met-before-the-deploy`, `bench:oidc_label_client_provisioned#stage-1-entry-created`, `bench:app_takes_the_outgoing_mail#stage-3-the-provider-change-follows`, `bench:mailserver_after_domain#stage-6-relay-password-reaches-the-mail-server`, `bench:mailserver_after_domain#stage-7-spamhaus-key-reaches-rspamd` |
| A secret that Catena keeps on the server reaches an application only when the application's `x-catena` block lists it and its compose file is the catalogue's unchanged file, and a secret that gives access to the server itself (the backup, Cloudflare, licence and admin credentials) never reaches an application | **no bench yet** |
| An application's declared verify command reports whether the application works | `bench:host_network_app_ports#stage-3-verify-hook-ok` |
| Each application's verify command runs every hour, and a failure is named once in the maintenance log | **no bench yet** |
| A verify run while an application is starting answers that the application is starting, and the dashboard says so; once the start time the application's `x-catena` block allows has passed without an answer, the verify fails and the maintenance log names it | `bench:apps_folder_round_trip#stage-6-starting-until-ready` |
| An application that was changed by hand, or that holds a person's own version of its compose file, is not updated, and the dashboard says so | `bench:apps_folder_round_trip#stage-4-a-changed-folder-is-held`, `bench:app_origin_update#stage-5-changed-folder-is-held` |
| An application from Catena's catalogue whose compose file was changed by hand, and that uses a permission only the unchanged catalogue file grants, is kept as it is: it still starts and restarts with the files it has, receives no new values, says why on its page, and every other application carries on as usual | `bench:monitoring_apps_from_the_catalog#stage-14-a-monitor-changed-by-hand-holds-only-itself` |
| An application from Catena's catalogue whose compose file was changed by hand still joins the networks of the services its file provides or reads, whatever else the server runs, and it provides or reads a service reserved for Catena's monitors only in the role its catalogue file gives it | `bench:monitoring_apps_from_the_catalog#stage-14-a-monitor-changed-by-hand-holds-only-itself` |
| When a default application's compose file was changed by hand, only that application is kept as it is: the configuration run, a restore and a move to a new server end well, the application runs with the files it has and gets its URLs and certificate on this server's domains, the applications that read values from it keep the values they have, and it receives new values again once its file matches the catalogue's | `bench:monitoring_apps_from_the_catalog#stage-14-a-monitor-changed-by-hand-holds-only-itself`, `bench:wizard_migrate_round_trip#stage-1b-an-application-changed-by-hand`, `bench:wizard_migrate_round_trip#stage-6c-the-application-changed-by-hand-came-held-and-runs` |
| The maintenance leaves alone an application that is stopped or that was changed through another tool | **no bench yet** |
| An update moves together all the services of an application that use the same image, runs the application's declared migration once the application answers, and keeps the new version | `bench:app_update_migrates_and_rolls_back#stage-5-moved-together-and-migrated` |
| An update moves an application to a newer build that its update policy allows, and the maintenance log names the update | `bench:nightly_update_real#stage-3-after-the-night` |
| An update takes a new version only once it has been out for the wait set on the Schedules page; a version published again does not restart the wait | **no bench yet** |
| An application pinned on nightly builds, whose update policy allows major versions, moves onto its newest stable release, with all its images in one deploy | `bench:tag_scheme_migration#stage-3-moved-together` |
| An application update, scheduled or started by hand, moves the application at most one major version at a time | `bench:app_origin_update#stage-4b-one-major-at-a-time` |
| An administrator can move an application added from a repository to a newer version from the application's page at any time after its first start, on every edition; the page names the version the application runs and the version the update moves it to | `bench:app_origin_update#stage-4c-the-update-section-names-both-versions` |
| An application's update shows on the application's page, step by step, from the moment the button is pressed until the update ends, and pressing the button again while the update runs starts nothing | `bench:app_origin_update#stage-4c2-the-update-is-followed-through-the-page` |
| An update whose repository cannot be read fails and says why on the application's page | **no bench yet** |
| A failed update of an application or of a service Catena runs puts the previous version and its data back by itself | `bench:app_update_migrates_and_rolls_back#stage-3-rolled-back-with-its-data`, `bench:infra_stack_update_rollback#stage-4-keycloak-rolls-back-with-its-data`, `bench:infra_stack_update_rollback#stage-7-a-connector-that-never-connects`, `bench:control_plane_update_rollback#stage-3-auto-recovery` |
| A rolled-back update is recorded with its version, and that version is added once to the list of quarantined versions, which no later update takes | `bench:fi_u5_persistent_quarantine#stage-3-durable-quarantine`, `bench:fi_u6_full_rollback_state#stage-3-state-records-rollback`, `bench:control_plane_update_rollback#stage-3-auto-recovery` |
| A successful update of a service Catena runs keeps the service's data, and the next converge leaves the service on the new version | `bench:infra_stack_update_rollback#stage-5-keycloak-keeps-a-good-bump` |
| A patch update of the sign-on service, the infrastructure Postgres or the proxy is kept only once Catena can still sign in to the sign-on service, query the database or reach an application through the proxy; an update that fails this check is rolled back with its data and its version is blocked from later updates, and an update whose check already fails on the running version is skipped | `bench:infra_stack_update_rollback#stage-4b-a-keycloak-catena-cannot-use-rolls-back`, `bench:infra_stack_update_rollback#stage-6-postgres-moves-with-its-rows` |
| A service that an administrator scaled to zero is left out of the updates, and its version is not blocked from later updates | **no bench yet** |
| What a person built inside an application survives an update and a rollback | `bench:app_update_migrates_and_rolls_back#stage-3-rolled-back-with-its-data`, `bench:app_update_migrates_and_rolls_back#stage-5-moved-together-and-migrated` |
| When an application's template keeps one of its services off the automatic updates, the application's tile on the Apps page shows the newer version that service can move to, and the service moves only when an admin confirms that version, on every edition; the move keeps the service's data and its exemption from automatic updates, and the service goes back to its previous version and data when the server's health drops after the move | `bench:pinned_service_update#stage-2-nightly-names-the-newest`, `bench:pinned_service_update#stage-4-moved`, `bench:pinned_service_update#stage-7-lapsed-licence-keeps-the-move`, `bench:pinned_service_update#stage-8-rolled-back-on-a-lapsed-licence` |
| On every edition, a check that runs once a day finds the newer version that each service kept off the automatic updates can move to, chosen the same way the maintenance chooses versions, and shows it on the application's tile on the Apps page without moving anything | `bench:pinned_service_update#stage-9-the-daily-report-names-the-move-on-community` |
| An application cannot write while its database moves to a new major version, and an admin can revert the application to its previous files and data | `bench:app_database_upgrade` |
| When Sync all, or a converge started from the dashboard or by a schedule, starts a dashboard sync, the sync runs under the server lock they already hold, so it writes the sign-in values, the mailboxes and the application states right away | `bench:oidc_label_client_provisioned#stage-3-secret-change-followed`, `bench:mailbox_sync_ownership#stage-3-sync-adds-and-records`, `bench:apps_folder_round_trip#stage-4-a-changed-folder-is-held`, `bench:app_origin_update#stage-5-changed-folder-is-held` |
| A revert, the rollback of a failed update and the recovery of an interrupted update put the copied data back before the previous version starts, and a deploy that does not end within ten minutes fails and releases the server lock | `bench:app_database_upgrade#stage-3-revert-puts-the-files-and-data-back` |
| When Stop or Delete says it is done, the application's containers and network are already gone, so Start or Add works right away | `bench:apps_folder_round_trip#stage-3-stop-and-start-keep-the-data`, `bench:apps_folder_round_trip#stage-5-delete-erases-the-data`, `bench:suite_wiring_commands` |
| When a monitor stops answering, the external heartbeat, the signal the server sends to a monitoring service outside it, names that monitor to the outside service | `bench:external_heartbeat_round_trip#stage-3-a-silent-monitor-is-named` |
| Gatus checks each shared service, including Beszel's hub | `bench:gatus_probes_beszel_hub` |
| A new server installs Gatus, Healthchecks and Beszel as applications from Catena's catalogue, at the versions its release names, and each one passes its own check | `bench:monitoring_apps_from_the_catalog#stage-1-the-monitors-are-catalog-apps`, `bench:monitoring_apps_from_the_catalog#stage-2-each-monitor-verifies`, `bench:monitoring_apps_from_the_catalog#stage-12-the-served-monitors-are-the-commits-the-release-names` |
| Gatus watches Catena's own services and the applications, and the dashboard counts what Gatus reports | `bench:monitoring_apps_from_the_catalog#stage-3-the-status-monitor-watches-catena-and-an-app`, `bench:monitoring_apps_from_the_catalog#stage-4-the-dashboard-counts-what-the-status-monitor-says` |
| Each scheduled job reports to Healthchecks, and the dashboard lists the jobs that Healthchecks holds | `bench:monitoring_apps_from_the_catalog#stage-5-a-scheduled-job-reaches-the-job-monitor`, `bench:monitoring_apps_from_the_catalog#stage-6-the-panel-reads-the-job-monitor` |
| Beszel lists the server it runs on | `bench:monitoring_apps_from_the_catalog#stage-7-the-resource-monitor-lists-this-server` |
| The URLs of Gatus, Healthchecks and Beszel send a visitor to sign in and let an administrator in | `bench:monitoring_apps_from_the_catalog#stage-8-each-monitor-answers-through-its-sign-in-gate` |
| Gatus, Healthchecks or Beszel, once removed by an administrator, is not installed again by the next converge, and nothing alerts about it | `bench:monitoring_apps_from_the_catalog#stage-9-a-removed-monitor-stays-removed` |
| Gatus, Healthchecks or Beszel, once added back from the catalogue, is connected to Catena again by the next converge | `bench:monitoring_apps_from_the_catalog#stage-10-added-back-it-is-wired-again` |
| A Catena update moves Gatus, Healthchecks and Beszel up to the versions its release names, and never down | `bench:monitoring_apps_from_the_catalog#stage-11-a-newer-release-tag-lifts-the-app` |
| Admins see each of the status, job and resource monitors once, among Catena's own services, with the version each one runs and its settings page | `bench:monitoring_apps_from_the_catalog#stage-13-each-monitor-shows-once-among-catenas-services` |
| The single sign-on signing keys, the dashboard's session cookie secret and the sign-in gate's client secret can be replaced without breaking sign-in | `bench:keycloak_signing_keys_rotation_round_trip`, `bench:oauth2_proxy_cookie_rotation_round_trip`, `bench:fi_a2_oidc_secret_rotation#stage-1-rotate-secret-reconverges`, `bench:fi_a2_oidc_secret_rotation#stage-2-keycloak-client-secret-rotated` |
| Every admin page and admin tool sits behind a sign-in gate, and the dashboard refuses a staff member every admin page and every admin action | `bench:app_container_cannot_claim_an_identity`, `bench:gatus_probes_beszel_hub#stage-1-hub-sends-anonymous-to-sign-in`, `bench:fi_a5_wrong_group_assignment#stage-2-admin-host-rejects-staff`, `bench:fi_a5_wrong_group_assignment#stage-3-staff-host-allows-staff`, `bench:admin_staff_cannot_reach_admin_routes#stage-1-admin-pages-refuse-staff`, `bench:admin_staff_cannot_reach_admin_routes#stage-2-admin-actions-refuse-staff`, `threat:CV2` |
| An application can be deleted only while it is stopped | `bench:apps_folder_round_trip#stage-7-delete-needs-a-stopped-app` |
| Before deleting an application, the control panel names the date of the latest backup that holds it; when no backup holds it, the person must type the application's name to delete it | `bench:apps_folder_round_trip#stage-5-delete-erases-the-data` |
| An application's card shows the logo its package ships only after the server has removed anything in it that could run a script or load something from another URL, and the logo is served from the control panel's own URL | `bench:apps_folder_round_trip#stage-9-a-package-logo-is-cleaned-and-shown` |
| An application's page shows what the application is, its setup steps and its longer notes in the words its package carries, in English or French; that text runs no script, loads nothing from another site and links only to https URLs | `bench:apps_folder_round_trip#stage-25-the-page-shows-the-package-texts-safely` |
| An administrator reads and changes an application's settings in one text editor that shows the application's settings file; the editor shows the values only in the answer to a request, and the log records that request without the file's text | `bench:apps_folder_round_trip#stage-11-the-files-open-whole-in-a-recorded-answer` |
| Once an application has started, changing a value that Catena generated and that the application keeps its own copy of is saved only after a confirmation; before the first start, and for the other values Catena generated, no confirmation is asked | `bench:apps_folder_round_trip#stage-23-each-generated-value-says-what-a-change-does` |
| Saving an application's settings file writes the file and nothing else: the application keeps running as it was, or stays down, until it is deployed again | `bench:apps_folder_round_trip#stage-13-an-edited-settings-file-is-deployed-and-kept` |
| The Redeploy application button, on the page of a running application, deploys the application again with its settings file and compose file as they are and with the newest image of each version they name; services whose definition did not change keep running | **no bench yet** |
| Before an application's first start, an administrator can switch it to another version from its repository, and the settings the administrator changed are kept; after the first start, only updates change its version | `bench:apps_folder_round_trip#stage-14-the-version-changes-only-before-the-first-start` |
| Adding an application answers on the Add button, which turns green, reads Added and links to the application's settings | `bench:apps_folder_round_trip#stage-15-an-add-answers-on-its-button` |
| The add page shows each repository's applications as the server last read them, reads a repository again once a day has passed or when an administrator asks, and downloads an application's files only when the application is added | `bench:apps_folder_round_trip#stage-26-the-lists-are-read-once-a-day` |
| An application that is being added shows on the Applications page of every administrator from the moment the add starts, with what it is waiting for, and its own card takes its place once the add ends | `bench:apps_folder_round_trip#stage-27-an-add-shows-at-once-in-the-list` |
| An application's page follows the application's start to the end, and the reason a change failed stays on the page until the next change of that application | `bench:apps_folder_round_trip#stage-16-a-start-is-followed-to-its-end` |
| An administrator can read the newest log lines of each service of an application on the application's page, for all services together or for one service chosen from a list, or follow them live; each service's own newest lines show however much the other services write, each viewing is recorded, the values that the settings file keeps secret show as ***, and following stops soon after the page closes | `bench:apps_folder_round_trip#stage-19-the-logs-show-each-service-without-its-secrets`, `bench:apps_folder_round_trip#stage-20-a-follow-ends-when-nobody-watches` |
| Every console of the dashboard follows the newest line until you scroll up, offers search, copy and line wrap, and is made taller, shorter, wider or narrower by dragging its bottom-right corner | **no bench yet** |
| A line that an application or a command writes shows on the dashboard with its colours and text styles, but never as a link, an image or a cursor movement | **no bench yet** |
| A setting that the application's package marks for the first start only can be changed until the application first starts, and is refused after that | `bench:apps_folder_round_trip#stage-8-a-first-start-setting-is-locked-after-the-start` |
| A staff member's application tiles carry no badge | `bench:admin_apps_tab_staff_filtered#stage-1-staff-tiles` |
| The Apps tab shows a staff member only the tiles their groups open and no admin tab, and shows an admin every tile and every tab | `bench:admin_apps_tab_admin_full#stage-1-every-tile`, `bench:admin_apps_tab_admin_full#stage-2-every-tab`, `bench:admin_apps_tab_staff_filtered#stage-1-staff-tiles`, `bench:admin_apps_tab_staff_filtered#stage-2-no-admin-tab` |
| Groups made in the dashboard are honoured by the sign-in gates; a destructive group change applies only once the impact shown is confirmed, a group change sent from another subdomain of the domain is refused, and the built-in groups for administrators, staff and clients cannot be renamed | `bench:identity_group_gates_access#stage-4-stale-confirmation-refused`, `bench:identity_group_gates_access#stage-5-a-sibling-subdomain-cannot-post`, `bench:identity_group_gates_access#stage-6-rename-through-the-edge`, `bench:identity_group_gates_access#stage-9-sign-in-tiers-keep-their-names`, `bench:identity_group_gates_access#stage-10-matching-confirmation-applies` |
| A person gets an account only by an invitation from the dashboard: the account is made in the group chosen, with the application access chosen (for clients only), and the person confirms their email address and sets their own password from the email | `bench:identity_group_gates_access#stage-11-invitation-makes-the-account`, `bench:identity_group_gates_access#stage-12-password-link-sent-again` |
| Access groups, the groups that let accounts into one application, keep the names Catena gives them, and a staff or administrator account is never a member of one | `bench:identity_group_gates_access#stage-13-access-groups-are-reserved`, `bench:identity_group_gates_access#stage-11-invitation-makes-the-account` |
| While outgoing mail is off, no invitation is made, and an invitation whose email cannot be sent leaves no account | **no bench yet** |
| Changing the admin email moves the admin's accounts in the sign-on service and in the monitors to the new email address, and keeps the password | `bench:admin_email_change_follows#stage-2-one-administrator-moved`, `bench:admin_email_change_follows#stage-3-keeps-its-password`, `bench:admin_email_change_follows#stage-4-monitors-follow` |
| The sign-on service's own admin account locks out repeated wrong passwords with a growing pause and is asked to set up a one-time code; a converge keeps both, and the documented server commands lift a lockout or replace a lost code | `bench:keycloak_master_realm_hardening#stage-1-master-realm-posture`, `bench:keycloak_master_realm_hardening#stage-2-wrong-passwords-lock-with-a-growing-pause`, `bench:keycloak_master_realm_hardening#stage-3-lockout-lifted-from-the-server`, `bench:keycloak_master_realm_hardening#stage-5-next-sign-in-asks-for-the-code`, `bench:keycloak_master_realm_hardening#stage-6-converge-through-the-automation-client`, `bench:keycloak_master_realm_hardening#stage-7-lost-code-asked-again` |
| An admin who lost access to both their email and their second factor creates a new, named admin account for the sign-on service from the server | `bench:keycloak_admin_email_loss_recovery#stage-2-recovery-admin-authenticates`, `bench:keycloak_admin_email_loss_recovery#stage-3-second-factor-asked-when-required` |
| A user who lost their password or second factor is reset from the server and sets a new one at the next sign-in | `bench:user_recovery_kcadm_temp_password`, `bench:user_recovery_2fa_reset` |
| A client application's container cannot send forged network frames to a Catena service on the network they share, and the app checker reports a compose file that keeps the raw-packet capability | `bench:client_container_raw_network#stage-1-no-link-layer-forgery-on-the-shared-network`, `bench:client_container_raw_network#stage-2-hand-deployed-file-is-flagged` |
| A sign-in cookie that a client application or another admin tool sees opens no admin tool | `bench:app_container_cannot_claim_an_identity`, `threat:CV2` |
| No sign-in gate shows its credentials in its Docker service definition | `bench:oauth2_proxy_cookie_rotation_round_trip`, `threat:CV7` |
| The dashboard runs a newly shipped action without a converge first, and refuses an action it does not know | `bench:payload_action_dispatches_without_converge#stage-3-new-name-no-converge`, `bench:admin_action_unknown_rejected`, `bench:ce_admin_actions` |
| The dashboard loads every script, style sheet and icon from its own site | `bench:ce_admin_smoke#stage-8-own-origin-assets` |
| The record of administrative actions can be exported and checked away from the server, and an edited or shortened record fails the check | `bench:audit_chain_tamper_evident`, `threat:CV10` |
| Each application's user sync reaches the identity provider through the dashboard on the server's loopback interface, and keeps running while the Cloudflare tunnel is down | `bench:mailbox_sync_ownership#stage-5-sync-without-the-tunnel` |
| Registering applications with the sign-on service keeps working while the Cloudflare tunnel is down | **no bench yet** |
| The dashboard forwards to the identity provider only the calls that the user sync makes, and only when a call carries the secret token the server generated for that sync; the dashboard adds no credential of its own | **no bench yet** |
| Settings > Outgoing mail sets the mail settings of the sign-in realm as long as the realm still holds the values Catena wrote; settings changed in the realm's own Email tab survive a converge and a change of the outgoing mail | `bench:ce_install_suite#stage-4h1-realm-mail-from-the-store`, `bench:realm_hand_edits_survive_converge#stage-2f-mail-save-keeps-the-email-tab` |
| Alerts reach the admin email through the outgoing mail | `bench:ce_install_suite#stage-4h2-alerts-reach-the-admin-by-email` |
| While outgoing mail is off, alerts reach only the channels added in Healthchecks | **no bench yet** |
| The subdomains of the dashboard and of the sign-in page are dashboard settings, and the subdomains of Gatus, Healthchecks and Beszel are settings of those applications; each subdomain uses its default name until another one is saved, and changing one moves every URL of the service to the new name, for Gatus, Healthchecks and Beszel the next time the server applies its configuration | `bench:infra_subdomain_change#stage-0b-an-untouched-save-stores-no-default`, `bench:infra_subdomain_change#stage-1-the-app-setting-takes-the-new-name`, `bench:panel_renames_itself#stage-2-change-the-name-through-the-panel`, `bench:panel_renames_itself#stage-4-converge-onto-the-new-name`, `bench:panel_renames_itself#stage-5-the-new-address-answers`, `bench:panel_renames_itself#stage-6-the-old-name-routes-nowhere` |
| A new dashboard URL saved in Settings is named on the page before the dashboard moves to it, and no URL is named while nothing moves | `bench:panel_renames_itself#stage-1-nothing-is-announced-while-nothing-moves`, `bench:panel_renames_itself#stage-3-the-page-names-where-it-is-going` |
| Admins see on each application's card the version it runs, taken from the server's own version report, and a staff member sees no version | `bench:versions_shown_to_administrators#stage-3-the-version-report-names-the-releases`, `bench:versions_shown_to_administrators#stage-4-admin-tile-names-the-version`, `bench:versions_shown_to_administrators#stage-5-staff-tile-names-none` |
| The System tab shows the server's own processor readings and links to the resource monitor | `bench:catena_admin_resources_tab_links_beszel#stage-1-resources-redirects-into-system`, `bench:catena_admin_resources_tab_links_beszel#stage-2-system-links-the-monitor`, `bench:catena_admin_resources_tab_links_beszel#stage-3-system-shows-this-servers-readings` |
| Gatus names each service without its version, so the service's history survives updates | **no bench yet** |
| The administrator the installer creates can open the admin console of the sign-in realm, and a role removed by hand stays removed | `bench:keycloak_master_realm_hardening#stage-8-realm-admin-opens-the-realm-console` |
| The resource monitor opens already signed in for an administrator who passed its sign-in gate, refuses a call from another site, and keeps its own password sign-in for when the sign-on service is down | `bench:gatus_probes_beszel_hub#stage-6-the-gate-signs-the-admin-in`, `bench:gatus_probes_beszel_hub#stage-7-a-forged-email-signs-no-one-in`, `bench:gatus_probes_beszel_hub#stage-8-a-cross-site-call-is-refused`, `threat:CV2` |
| Each Settings section shows whether its last check passed after its last save, and a passing check stands until the values change; a value changed outside the dashboard clears that result | **no bench yet** |
| A Settings section reads Pending only while its own saved change waits for the server, and a change saved in another section leaves it as it is | `bench:ce_install_suite#stage-3a7b-a-time-zone-save-leaves-other-sections-applied` |
| A Settings section that was never saved reads Default when it has a default value and Not set when it has none | **no bench yet** |
| When the server's check after a configuration run fails, the Settings section that the failed check judges reads Failed, or, when the check judges no section, every section saved since the last run that ended well reads Failed; the step that failed and its log stay in Settings, also after a later run starts | **no bench yet** |
| Outgoing mail sends a test message to the admin email address on request | **no bench yet** |
| With Catena Business, each change made through the panel appears in the Log tab with who made it and whether it was refused | **no bench yet** |

## Paid features

This section describes how the dashboard features that a paid edition unlocks
behave. The website's [edition comparison](https://catena.run/en/#pricing)
lists which edition unlocks which feature.

| Invariant | Enforced by |
| --- | --- |
| Each maintenance button that a licence unlocks starts its own action | `bench:ee_named_buttons#stage-1-audit-export`, `bench:ee_named_buttons#stage-3-verify-backup`, `bench:ee_named_buttons#stage-4-daily-run-now` |
| Each administrative action reaches the central log collector intact, and is also recorded in the server's own hash chain | `bench:ee_audit_ship#stage-3-loki-assert`, `bench:ee_audit_ship#stage-4-dual-sink` |
| The monthly compliance attestation is signed from the server's own evidence, carries the head of the server's audit chain and its key, verifies against the published key, and a tampered attestation is refused | `bench:ee_attest#stage-1.5-host-audit-digest`, `bench:ee_attest#stage-2-sign`, `bench:ee_attest#stage-3-verify` |
| The compliance panel lists every control of its registry in English and French and filters them by framework; its attestations form a hash chain that verifies, it refuses an attestation of a control that Catena measures itself and a post from another site, and its records survive a redeploy and are in every backup and every export | `bench:ee_compliance_panel#stage-2-panel-renders-en-fr`, `bench:ee_compliance_panel#stage-3-framework-filter`, `bench:ee_compliance_panel#stage-5-attest-manual-control`, `bench:ee_compliance_panel#stage-8-cross-origin-refused`, `bench:ee_compliance_panel#stage-9-persists-across-redeploy`, `bench:ee_compliance_panel#stage-10-covered-by-backup`, `bench:ee_compliance_panel#stage-11-export-carries-the-chain` |
| The monthly report shows, in English and French, the month's backups, restore tests, updates, vulnerabilities, availability and alerts from the server's own records, and names what the server does not record | `bench:monthly_report_renders#stage-3-the-report` |
| The identity check reports unhealthy while multi-factor sign-in is off, and healthy once the dashboard turns it on | `bench:ee_identity_probe#stage-2-drift`, `bench:ee_identity_probe#stage-3-healthy` |

## CI

`ci.yml` runs the unit tests of the Ansible tree and of the installer, tests
the installer's entry point and its wheel, builds the sign-on extension, checks
the Ansible syntax, and runs the ratchets on copy-paste, sentences and prose,
checks that fail when a change adds to what they count. `security.yml` runs
scanctl. `publish-installer.yml` publishes the installer to PyPI from a release
tag. Pinned images move to new versions through the engine bump that
catena-admin runs every night; every other dependency follows
[renovate](https://github.com/catenahq/renovate) and
[renovate-config](https://github.com/catenahq/renovate-config), and
[scanctl](https://github.com/catenahq/scanctl) scans each update before it
merges.

| Invariant | Enforced by |
| --- | --- |
| Every workflow runs on each pull request and on each push, except pushes to the Renovate, engine-bump and scanctl branches, whose pull requests test every push | `workflow:ci.yml`, `workflow:security.yml` |
| No secret is in the repository's files or in its history | `scanctl:gitleaks`, `workflow:security.yml#scanctl`, `threat:CP1` |
| No env file is tracked in git, and `.gitignore` carries the rule that ignores env files | `workflow:ci.yml#prose` |
| This repository holds no licensed source code and no Go code | `audit:check-port`, `audit:check-grid`, `threat:CP3` |
| Every dependency and every pinned image has to pass a vulnerability scan | `scanctl:osv-scanner`, `scanctl:trivy`, `workflow:security.yml#scanctl`, `threat:CP5` |
| A known vulnerability (CVE) that has a fix in a pinned image fails the weekly scheduled scan until a newer pin clears it or a dated acceptance covers it | `scanctl:trivy`, `workflow:security.yml#scanctl` |
| Static analysis runs on every change | `scanctl:semgrep`, `scanctl:zizmor` |
| GitHub Actions are pinned by digest, and a tag moved to another commit is not adopted on its own | `scanctl:zizmor`, `threat:CP6` |
| The installer builds, passes its lint checks and passes its tests | `workflow:ci.yml#installer`, `workflow:ci.yml#entry-points`, `workflow:ci.yml#syntax`, `workflow:ci.yml#duplication`, `workflow:ci.yml#sentences` |
| A release tag on main publishes the tested installer as `catena-installer` through trusted publishing, with its version taken from the tag | `workflow:publish-installer.yml#publish` |
| The sign-on extension passes its tests and builds reproducibly: two builds give the same bytes | `workflow:ci.yml#keycloak-provider` |
| Code comments describe the code as it stands and hold no history | `workflow:ci.yml#prose` |
| Core code names no application, except in a recorded debt that can only shrink | `workflow:ci.yml#prose` |
| Public text names no system that Catena does not ship | `audit:check-banned-words` |
| Every file and every scenario is classified against the feature manifest, and none is left unclassified | `audit:check-grid`, `audit:check-all` |
| A gate pointer in this file that does not resolve, or a rehearsal scenario that this file does not name, fails the build | `audit:check-public-specs`, `threat:CP7` |

## Gate pointers

`bench:<scenario>` is a rehearsal scenario, a test that drives the real product
on disposable virtual machines, and `bench:<scenario>#<stage>` is the stage of
that scenario that proves the claim. `audit:<gate>` is a static check of the
maintainers' audit graph, run in CI and before every rehearsal.
`workflow:<file>#<job>` is a CI job in this repository. `scanctl:<tool>` is a
scanner of the bundled security workflow. `threat:<ID>` is an invariant in the
maintainers' threat-model register. A pointer that does not resolve, or that
names a retired invariant, fails the build, and so does a scenario that this
file does not name.

## Declared, not yet built

Everything this file declares that the code in this repository does not
implement is listed here.

None.

## Installation

A server needs at least 4 GB of memory: 6 GB for light applications with few
users, and 8 GB for heavier applications with many users. Catena's own services
take about 2 GB. See [README.md](README.md).

| Invariant | Enforced by |
| --- | --- |
| Catena installs and runs on a server with less than 8 GB of memory, and the shared antivirus scanner reloads its virus signatures without keeping two copies of them in memory | `bench:ce_install_suite`, `bench:clamd_reload_on_a_small_host`, `workflow:ci.yml#installer` |

## Scenarios outside the invariants

Every rehearsal scenario is named in this file. The scenarios below prove no
invariant: a `skipped` scenario never runs its checks, and an `na` scenario
checks the bench, a mock or a third-party tool instead of the product.

| Scenario | Status | Why |
| --- | --- | --- |
| `bench:cloudflare_api_rotation_round_trip` | skipped | Needs a new Cloudflare token created for the run |
| `bench:tailscale_oauth_rotation_round_trip` | skipped | Needs a new private-network OAuth client created for the run |
| `bench:smtp_rotation_round_trip` | skipped | Needs a new mail-provider key created for the run |
| `bench:rotate_all_secrets_sequencer` | skipped | Waits until every scenario that rotates a single secret runs |
| `bench:dev_to_prod_cutover_round_trip` | skipped | Waits for the tools that move each application to its new URL after the domain changes |
| `bench:debian_major_upgrade_restore` | skipped | Needs virtual machine images on two Debian major releases |
| `bench:pg_major_version_cross_restore` | skipped | Needs virtual machine images on two Postgres major versions |
| `bench:healthchecks_self_host_loss` | skipped | Waits for stages that wipe the server's job monitoring and watch the external heartbeat |
| `bench:fi_t3_start_sweep_failure` | skipped | Covered by the bench's unit tests |
| `bench:measure_converge` | na | Measures how long a converge takes |
| `bench:mixed_template_negative_restore` | na | Checks the bench's fixture engine |
| `bench:fi_c8_app_init_loop` | na | Checks the bench's diagnostics |
| `bench:fi_o4_hosts_yml_malformed` | na | Checks the YAML parser |
| `bench:fi_s2_input_truncated` | na | Checks the bench's configuration loader |
| `bench:fi_n2_cf_tunnel_down` | na | Checks the bench's Cloudflare preflight |
| `bench:fi_a3_keycloak_unreachable` | na | Checks a probe against a mock |
| `bench:fi_b6_healthchecks_down` | na | Checks a probe against a mock |
| `bench:fi_n3_dns_propagation_lag` | na | Checks a probe against a mock |
| `bench:fi_u3_gatus_baseline_down` | na | Checks a probe against a mock |
| `bench:fi_u4_ovh_rate_limited` | na | Checks a probe against a mock |
| `bench:fi_v2_external_scan_blocked` | na | Checks a probe against a mock |
| `bench:fi_s7_smtp_creds_invalid` | na | Checks that the mail relay refuses a bad login |
| `bench:fi_b4_locked_pack_rotation` | na | Checks object lock on the bench's storage server |
| `bench:worm_object_lock_expiry_edge` | na | Checks object lock expiry on the bench's storage server |
| `bench:fi_d7_restic_corrupt_pack` | na | Checks restic's own data check |
