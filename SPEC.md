# SPEC.md

Catena-CE is an Ansible-based installer for Catena. It prepares a new Debian 13 server to host dockerized applications by providing a secure, ready-to-deploy platform. The main dashboard, `catena-admin`, is built from a closed-source repository and its image is available [on Github](https://github.com/catenahq/catena-admin/pkgs/container/catena-admin)

This file is the contract, and it leads the code: the tree implements what
this file states. Each section states its scope in a few lines and
its behaviour as invariants, each with the gates that prove it
([Gate pointers](#gate-pointers)). An invariant marked **no bench yet** has no
rehearsal scenario proving it; a scenario that proves no invariant is listed in
[Scenarios outside the invariants](#scenarios-outside-the-invariants). Items declared here that the tree does not
implement yet are listed in [Declared, not yet built](#declared-not-yet-built).

## Not in this repository

- **The dashboard source.** `catena-admin` is closed. This repository deploys
  its public signed image and extracts the host engines from that same image.
- **The application catalogue.** Per-app compose files and templates live in
  `catena-templates`.
- **Maintainer tooling.** The rehearsal bench, the audit graph and the
  attestation are not published.
- **Secrets.** The repository contains none. What a deployment needs is
  documented in [ansible/SECRETS.md](ansible/SECRETS.md).

## Licensing

Catena-CE is complete on its own. Catena Pro and Catena Business are Polar
subscription keys, saved in the dashboard, that unlock dashboard features; the
website's [edition comparison](https://catena.run/en/#pricing) lists which.

| Invariant | Enforced by |
| --- | --- |
| A server with no key, or with a key Polar refuses, runs as Community, and everything Community offers keeps working | `bench:activate_ee#stage-1.5-negatives`, `bench:ee_lapse#stage-4-freeze`, `bench:license_seat#stage-6-a-fails-open`, `threat:LE3` |
| A Pro key unlocks the Pro features and no Business feature: the Business pages answer "not found" and the server refuses the Business actions | `bench:ee_entitlement_partial` |
| The host asks Polar about its key every hour, and only while it holds one | **no bench yet** |
| A key is active on one server at a time, bound to its hardware: a copy of a licensed server and a second server are refused while the first holds it | `bench:license_seat#stage-2-a-copy-is-refused`, `bench:license_seat#stage-3-a-second-server-is-refused`, `bench:polar_sandbox_smoke#stage-4-second-server-refused`, `threat:LE7` |
| When Polar takes a seat for a server but its answer never reaches the server, the seat is labelled in Polar with the server's domain and the server's licence status names the unanswered activation; once that seat is freed in Polar and the key saved again, the server is licensed | `bench:license_seat#stage-4.6-an-unanswered-activate-is-named` |
| When Polar holds an activation of the key that the server does not know of, the dashboard says to free it in Polar's customer portal | **no bench yet** |
| While Polar cannot be reached, a licensed server keeps its paid features for 48 hours after Polar last confirmed its key, then locks them, and unlocks them once Polar answers | `bench:polar_unreachable_grace`, `threat:LE3` |
| A server whose key is revoked, or whose seat is freed, in Polar locks its paid features at its next check, with no grace period | `bench:ee_lapse#stage-2-revoked-at-polar`, `bench:license_seat#stage-5-the-first-locks` |
| A server whose key is cancelled in Polar locks its paid features at its next check, with no grace period | **no bench yet** |
| When a server locks its paid features, the admin is told once: one entry in the maintenance log and one email through the outgoing mail | `bench:ee_lapse#stage-5-one-lock-notice`, `bench:polar_unreachable_grace#stage-3-past-the-grace` |
| A server that locks its paid features keeps everything else: the scheduled backup and vulnerability watch, the OS updates, restores and Community actions keep working | `bench:ee_lapse#stage-3-host-lapsed`, `bench:license_seat#stage-6-a-fails-open`, `threat:LE3` |
| Community actions keep working on a licensed host, and leave its licence state unchanged | `bench:ee_ce_regression#stage-2-press-ce-action`, `bench:ee_ce_regression#stage-4-business-still-active` |
| The Polar API version the licence check pins still answers in Polar's sandbox | `bench:polar_api_version_current#stage-1-pinned-version-answers` |
| On a server whose paid features are locked, the applications keep running with their data, and a backup, a restore and an update started by hand still run | **no bench yet** |
| Moving a server to a new machine needs no paid edition on the new machine | `bench:wizard_migrate_round_trip#stage-0-preconditions` |
| Moving a server with no paid edition to a new machine is refused | **no bench yet** |
| During a move, the new server reaches the old one over SSH only, with a one-time migration ticket and the pairing code the old server's dashboard shows, and only while the old server's migration window is open; no port opens and no private network is needed | `bench:migrate_lane_auth_denied`, `bench:wizard_migrate_round_trip`, `threat:CV15` |
| During a move, the licence key moves only once the move is validated: the old server frees the key and closes its migration window, then the new server takes the key and converges | `bench:migrate#stage-1-migrate`, `bench:wizard_migrate_round_trip#stage-4-window-and-completion`, `bench:migrate_lane_auth_denied#stage-4.5-licence-release-guarded`, `bench:migrate_preseed_no_split_brain#stage-5-licence-stays-with-the-source`, `bench:wizard_migrate_resume_source`, `threat:LE12` |
| Error reports leave a paid server only while its admin has switched them on, carry only the check, its state and the application, are listed in the dashboard, and stop at once when switched off | `bench:error_reports_opt_in`, `threat:CV18` |
| A failed job's text reaches support only after the admin has reviewed it, and only as the admin edited it | `bench:error_reports_opt_in` |
| The dashboard offers a failed job's text for download | **no bench yet** |

## How this repository is layered

The installer reaches the server over SSH, and the server runs
`ansible/install-host.sh` from the release image's own tree. A playbook is one
atomic operation: hosts and an ordered list of roles. A role is the reuse unit,
with its defaults, templates, handlers and its own `validate.yml`. A task is one
step, shared wherever the step is the same. Reuse runs downward only. A
converge is a run of `converge.yml` or `reconcile.yml`: it applies the roles
and changes only what differs from what they declare.

## The installer

`catena-installer` (`installer/`) runs on the client's machine with uv
(`uvx catena-installer`) and only reaches and installs a server: SSH access, the
admin email, and what the first converge needs. Everything the dashboard can
change is set in the dashboard. Its verbs are `init`, `install`, `connect` and
`uninstall`; `ansible/seed.py` writes the inventory. `install` runs on the
server in two legs, each through its own SSH session: the accounts leg creates
the `ops` and `panel` accounts, and the system leg, logged in as `ops`, runs
the rest of the install.

| Invariant | Enforced by |
| --- | --- |
| On Windows, macOS and Linux, the installer run with no argument serves a page on the machine's loopback address, and run with a verb works in the console | `workflow:ci.yml#installer`, `workflow:ci.yml#entry-points`, **no bench yet** |
| The installer runs no Ansible: the server installs itself from the tree of the release image it fetched, so its roles, engines and dashboard are one release | `bench:update_from_previous_release#stage-4b-one-version-after-the-reinstall`, `bench:install_rerun#stage-4-engines-follow-the-shell`, `workflow:ci.yml#installer` |
| The installer asks only for what reaching and installing a server needs, and an install given a value the dashboard owns stops before writing anything | `workflow:ci.yml#installer`, **no bench yet** |
| `init` writes an inventory with every key at its default | `workflow:ci.yml#installer`, **no bench yet** |
| `install` reaches the server at its SSH address when one is set, else at its public IP; `--address` reaches another address for one run | `bench:launcher_install#stage-1-private-leg-installs`, `bench:launcher_install#stage-3-public-leg-installs`, `bench:install_rerun#stage-1a-address-is-for-this-run` |
| `install` run again asks nothing, changes nothing on a healthy server, and redeploys a dashboard that is gone | `bench:install_rerun#stage-1-install-rerun`, `bench:install_rerun#stage-6-install-deploys-the-panel` |
| `install` run again re-applies the release the server records, the bootstrap roles included | **no bench yet** |
| `install --release` moves the server to that release with that release's code | **no bench yet** |
| `install --tags` limits the install's converge to the roles named | **no bench yet** |
| A server reachable only through the provider's login installs in two legs: the accounts leg creates `ops` and `panel` with the installer's key and closes no login, then the system leg runs as `ops` | `bench:launcher_install#stage-1-private-leg-installs` |
| The system leg, whose first role turns off every other login, starts only once sshd records the installer's key opening an `ops` session | `workflow:ci.yml#installer`, **no bench yet** |
| An install leg whose SSH connection drops runs to its end on the server, and the server's log of the leg holds no password | `bench:fi_o2_operator_ctrl_c#stage-1b-interrupted-install-ends-on-the-server` |
| An install started while an earlier install leg still runs on the server waits for that leg to end | `workflow:ci.yml#installer`, **no bench yet** |
| After the system leg, the installer scans every TCP port of the server's public IP against the ports the server declares, and checks that the scan cannot reach the container network | `workflow:ci.yml#installer`, **no bench yet** |
| A server presenting another SSH host key than the one trusted for its address stops `install` before anything reaches it; `--reinstalled`, or the page's reinstalled-server box, forgets the old key and trusts the new one | `bench:launcher_install#stage-6-install-stops-on-the-new-key`, `bench:launcher_install#stage-7-reinstalled-flag-continues`, `bench:launcher_install#stage-8-new-key-trusted` |
| The inventory holds no credential | `bench:launcher_install#stage-2-private-leg-leaves-its-answers`, `bench:launcher_install#stage-4-public-leg-leaves-its-answers` |
| An admin password reaches the server as a 0600 file that lasts only for the run, and the installer generates no secret | `workflow:ci.yml#installer`, **no bench yet** |
| The dashboard answers through the `panel` account's forward to the host's loopback | `bench:ce_install_suite#stage-3-bare-install` |
| `connect` opens the `panel` account's forward to the dashboard from the client's machine | `workflow:ci.yml#installer`, **no bench yet** |
| `uninstall` hands the server back to Debian: Debian's package updates run on Debian's defaults again, and no container engine package stays held | `bench:ce_uninstall#stage-3-handback-asserts` |
| `uninstall` prints teardown guidance | **no bench yet** |
| A verb that runs one playbook is named after it | `audit:check-grid` |

## Playbooks

At install the server runs `accounts.yml`, `bootstrap.yml`, `show-keyset.yml`,
`converge.yml` and `validate.yml`. It runs `reconcile.yml` when it converges
itself, and, from the dashboard only, `lockdown.yml`, `rotate-tunnel.yml` and
`rotate-tailscale.yml`. `uninstall.yml` hands it back to Debian.

| Invariant | Enforced by |
| --- | --- |
| A converge run again repairs a server that drifted from what the roles declare, and never duplicates or breaks anything on a healthy server | `bench:install_rerun#stage-1-install-rerun`, `bench:converge_suite#stage-7c-rotated-secret-arrives`, `bench:converge_suite#stage-7b-bump-survives` |
| The host converges itself from the tree inside the dashboard image | `bench:reconcile_is_a_noop#stage-1-first-reconcile` |
| The host's own converge reads nothing from a controller inventory, and no installer verb runs it | `workflow:ci.yml#installer`, **no bench yet** |
| `reconcile.yml` applies the roles of `converge.yml` in the same order, minus the roles a person must run | `workflow:ci.yml#installer`, **no bench yet** |
| Validation checks every role's state and, from the server itself, the dashboard, the public hostnames once a domain is set, and the published ports; a converge ends with validation and fails when validation fails | `bench:ce_validate#stage-1-ce-validate` |
| Validation fails on an exposure the server does not declare | **no bench yet** |
| Validation is the install's last step and runs on its own | `workflow:ci.yml#installer`, **no bench yet** |
| `lockdown.yml` joins the server to the private network saved in the dashboard | `bench:ce_install_suite#stage-3a1-tailnet-joined`, `bench:fi_n11_lockdown_refuses_shields_up` |
| `lockdown.yml` runs only from the dashboard, and never during a converge | `workflow:ci.yml#installer`, **no bench yet** |
| The install generates the admin and console passwords when they are absent, and shows them with the journal verification key after its bootstrap step and again when it ends | **no bench yet** |
| An admin can make the server authenticate to its private network again from the dashboard | **no bench yet** |
| A converge that meets a frozen container engine or a badly degraded network fails within a bounded time and names the cause | `bench:fi_c1_docker_daemon_hang`, `bench:fi_c4_registry_pull_timeout` |
| A converge on a server whose disk is nearly full, from the install or from the dashboard, stops at its disk check before any role runs | `bench:fi_d5_disk_full_mid_converge` |

## Roles

A role is **Bootstrap** when nothing left could repair it if it were wrong: it is
applied by `install`, on the server, from the release image, and the set stays
small. Every other role is **Reconcile**: its value is a function of the product
version or of the host, and the host runs it unattended from the image. A role's
side is its directory, `ansible/bootstrap/roles/` or `ansible/reconcile/roles/`;
`ansible/boundary.yml` declares each role, the reason it is bootstrap, and how it
is reached.

Bootstrap: `common`, `host_hardening`, `tailscale`, `storage`, `docker`,
`catena_admin_host`, `ansible_runtime`. Reconcile, in converge order:
`dispatch_path`, `payload`, `public_ports`, `host_maintenance`, `swarm`,
`traefik`, `postgres`, `cloudflare_tunnel`, `keycloak`, `oauth2_proxy`,
`infrastructure`, `catena-admin`, `backup`. `tier1_stack` runs after them;
`cloudflare_tunnel_regenerate` runs only from `rotate-tunnel.yml`.

| Invariant | Enforced by |
| --- | --- |
| Every role is on exactly one side, with a stated reason if it is bootstrap | `audit:check-grid`, `workflow:ci.yml#installer` |
| No reconcile task writes a bootstrap-owned path (`/etc/ssh/`, `/etc/sudoers.d/`, `authorized_keys`, `/usr/local/sbin/catena-admin-runner`, `/usr/local/lib/catena-dispatch/`, `/etc/sysctl.d/`, `/etc/modprobe.d/`, `/etc/ansible/facts.d/`), and no reconcile play reaches a bootstrap task file; only `dispatch_path` changes how the dashboard's requests reach the host, and only after proving the new way works | `workflow:ci.yml#installer` |
| No bootstrap task writes a reconcile-owned path: the OS update and restart policy, the journal and clock drop-ins, the scheduled updates' configuration, the dashboard's port declaration and the overlay self-heal | `workflow:ci.yml#installer` |
| A restore runs only when started on the server, never from the installer and never during a converge | `workflow:ci.yml#installer`, `bench:ce_restore#stage-4-ce-restore` |
| The install's converge and the host's own converge produce the same host: after either, the other changes nothing | `bench:reconcile_is_a_noop` |
| A host that converges itself puts back the upkeep it owns: unattended upgrades, the journal's storage, the mail agent off port 25, the clock on its NTP pool, the swarm's task history and data-node label, and the configuration its scheduled jobs read | `bench:reconcile_repairs_host_upkeep#stage-3-restored`, `bench:reconcile_repairs_host_upkeep#stage-6-clock-on-the-pool` |
| The host resolves its own name, keeps the time zone and locale set in the dashboard, and runs git as `catena-git`, with no login and no home | **no bench yet** |
| Kernel and module hardening is in force | `bench:ce_install_suite`, `bench:ce_validate#stage-1-ce-validate` |
| Kernel hardening is applied before the container engine's first start | **no bench yet** |
| Data lives under one data prefix on the host's own disk | `bench:ce_install_suite` |
| A remote bulk mount holds data the host names | **no bench yet** |
| The container engine runs the version pinned here, held on the host | `bench:ce_uninstall#stage-1-mask-apt-timers`, `bench:docker_engine_upgrade_round_trip#stage-3-the-new-engine-holds` |
| The dashboard's dispatch signs in as the runner account with its own key pair and a host key it trusts; the migration account admits no key outside an open migration window | `bench:ce_admin_smoke#stage-1-host-state`, `bench:migrate_lane_auth_denied#stage-0-preconditions`, `bench:migrate_lane_auth_denied#stage-1-no-lane-before-arming`, `bench:wizard_migrate_resume_source` |
| A host whose Ansible runtime broke repairs it with no person | **no bench yet** |
| A dashboard update switches how the dashboard's requests reach the host only after a request has crossed the new path; when that proof fails, the converge stops and the running path stays | `bench:update_from_previous_release#stage-3e-the-dispatch-path-moved-beside-the-running-one`, `bench:update_from_previous_release#stage-3f-a-failed-proof-keeps-the-path` |
| A converge installs the host engines only from the dashboard image it names, by digest, built from this tree, and root runs nothing out of a place the dashboard can write | `bench:update_from_previous_release#stage-5-another-tree-is-refused`, `bench:ce_install_suite#stage-3d-payload-digest-gate`, `workflow:ci.yml#installer`, `threat:CV14` |
| A converge fails when the host engines it installs lack the port reconciler or its timer | **no bench yet** |
| After the container engine restarts, containers that came up without their overlay network rejoin it by themselves | `bench:swarm_overlay_selfheal` |
| A deleted container is started again by itself; a removed service stays removed until the next converge, which restores every declared service | `bench:container_delete_recreated#stage-1-app-task-rescheduled`, `bench:container_delete_recreated#stage-2-infra-task-rescheduled`, `bench:container_delete_recreated#stage-3-removed-service-stays-removed`, `bench:container_delete_recreated#stage-4-converge-restores-service`, `bench:container_delete_recreated#stage-5-declared-set-serves-again` |
| A host restarted without warning brings every declared service back, with no converge and no person | `bench:host_reboot_recovery#stage-3-host-booted`, `bench:host_reboot_recovery#stage-4-stack-back-without-a-converge`, `bench:host_reboot_recovery#stage-5-proxy-serves-again` |
| The sign-on service keeps its database on the infrastructure Postgres | **no bench yet** |
| Each admin tool behind a sign-in gate has a gate and a sign-in session of its own | `bench:app_container_cannot_claim_an_identity#stage-5-a-seen-cookie-opens-no-admin-tool` |
| With no Cloudflare token in the on-box store, the tunnel waits and the converge succeeds | `bench:ce_install_suite#stage-3-bare-install`, `bench:install_without_a_domain#stage-1-converge-with-no-domain`, `bench:post_compromise_rebuild#stage-6-tunnel-sync-without-a-token` |
| The tunnel is replaced only when an admin asks, and a converge leaves a healthy tunnel connector running untouched | `workflow:ci.yml#installer`, `bench:cf_tunnel_regenerate_round_trip#stage-1-rotate-tunnel`, `bench:fi_c6_cloudflared_flapping#stage-1-reconverge-no-flap` |
| The dashboard runs the release the install fetched or, on the host's own converge, the image it already runs | `bench:update_from_previous_release#stage-4b-one-version-after-the-reinstall`, `bench:install_rerun#stage-4-engines-follow-the-shell` |
| A converge that names no dashboard image stops before any role runs | **no bench yet** |
| An install fetches the release it is given, else the one the server records, else the newest the registry names | `workflow:ci.yml#installer`, **no bench yet** |
| The first backup runs once a destination is saved | `bench:ce_install_suite#stage-4i0-first-backup-ran` |
| The control-plane services' accumulated definition loads against the installed engine and names what runs, and applies nothing | `bench:ce_install_suite`, `workflow:ci.yml#installer` |

## State the tasks produce

Six things outlive the run that wrote them. Each belongs to the role or task
that produces it.

### The on-box store -- `common`, read by every role

`/etc/catena/config.json` is the runtime source of truth: the admin password, every
setting and credential saved in the dashboard, and every internal secret,
generated on the host. `ansible/helpers/onbox_config.py` declares which names are secrets.

| Invariant | Enforced by |
| --- | --- |
| The on-box store is readable by root alone (0600) and holds every secret | `bench:ce_install_suite#stage-3b-onbox-store`, `threat:CV3` |
| Every backup holds the on-box store, and a whole-server restore brings it back | `bench:dr_suite#stage-19-onbox-store` |
| A client who lost the install inputs rebuilds a usable set of credentials by reading the on-box store of the running server | `bench:recover_secrets_from_running_host` |
| A server broken into at root holds no credential that reaches another Catena server: its SSH keys are refused there, and no generated secret is the same on both | `bench:cross_host_pivot_finds_nothing#stage-2-ssh-pivot`, `bench:cross_host_pivot_finds_nothing#stage-3-store-custody`, `bench:cross_host_pivot_finds_nothing#stage-4-keycloak-master` |
| The backup's object-store keys appear on no dashboard page, and in no file or log of the server except the on-box store and the env file written from it | `bench:s3_backup_keys_rotation_round_trip#stage-6-no-secret-shown` |
| A converge limited to some roles reads each value from the on-box store, and a value left in the inventory changes nothing | `bench:fi_s3_placeholder_inert#stage-1-placeholder-inert-converge-succeeds`, `bench:fi_s3_placeholder_inert#stage-2-onbox-store-not-poisoned`, `bench:converge_suite#stage-7c-rotated-secret-arrives` |
| The inventory fills the on-box store at the first install only; after it, the development certificate keys are the one value every install writes from the inventory into the store | `bench:install_rerun#stage-8f-store-and-hosts-follow-the-env`, `bench:fi_s3_placeholder_inert#stage-2b-onbox-config-not-reseeded` |

### The public-port registry -- `public_ports`

A port is declared before it opens: roles drop fragments into
`/etc/catena/public-ports.d/`, application templates carry `vps.expose.*` labels,
and `catena-public-ports` merges both into ufw and DOCKER-USER rules. Web traffic
enters only through the Cloudflare tunnel. No domain, tunnel or private network
is set at install; each is saved in the dashboard.

| Invariant | Enforced by |
| --- | --- |
| The server's address has no web port open, and no port open outside the allowed set | `bench:ce_install_suite`, `bench:dr_suite#stage-14-post-recovery-assertions`, `threat:CV1` |
| An outside scan of the server's public names and address finds no critical or high vulnerability | `bench:security_scan#stage-7-emit-status` |
| Every open port is declared: role fragments and template labels merge into ufw and DOCKER-USER, and validation and the outside scan check against the merged set | **no bench yet** |
| An application's declared ports open with it and close when it is removed | `bench:host_network_app_ports#stage-2-ports-open`, `bench:host_network_app_ports#stage-7-removal-closes-the-ports` |
| The ports an application's labels declare are read with the label names of the dashboard image the server runs, so a dashboard image update changes how those labels are read with no converge | `bench:labels_vocabulary_from_image` |
| Port rules are reapplied on every container engine start, without waiting for the host lock | **no bench yet** |
| SSH is key-only, with no root and no password login | `bench:ce_install_suite`, `threat:CV6` |
| With no private network, public 22 is open and nothing closes it | `bench:ce_install_suite#stage-3-bare-install`, `bench:ce_install_suite#stage-3-lockdown-without-a-tailnet-keeps-22` |
| With a private network joined, public 22 stays open until the Lockdown | `bench:ce_install_suite#stage-3a2-end-state` |
| The Lockdown closes public 22 only once the private network proves it reaches the host | `bench:ce_install_suite#stage-3a4-lockdown-closes-22`, `bench:fi_n1_tailnet_partition_mid_converge`, `bench:fi_v3_tailscale_acl_misconfig`, `bench:fi_n11_lockdown_refuses_shields_up` |
| After a Lockdown, public 22 reopens while the private network is down | `bench:ce_install_suite#stage-3a6-tailnet-down-reopens-22` |
| Once the private network is set back to none and the change applied, public 22 opens | `bench:ce_install_suite#stage-3a7-no-provider-reopens-22` |
| An admin's SSH session survives the firewall being applied under it | `bench:fi_n8_ufw_concurrent_ssh` |
| A server installs with no domain, tunnel or private network, and a domain saved later in the dashboard brings its public addresses up through the tunnel without a reinstall | `bench:ce_install_suite#stage-3-bare-install`, `bench:ce_install_suite#stage-3a-tunnel-live`, `bench:install_without_a_domain` |
| A credential entered in the dashboard is checked on the host before it is stored, and a refused one is never stored | `bench:ce_install_suite#stage-3a3-revoked-token-refused`, `bench:fi_s4_tailscale_oauth_revoked#stage-1-f-s4-save-refused`, `bench:s3_backup_keys_rotation_round_trip#stage-2-pair-b-saved-in-settings` |
| A Cloudflare token the provider refuses fails the converge at the tunnel role | `bench:fi_n4_cf_zone_misconfigured` |
| Until a domain exists, the dashboard is reached through the forward-only `panel` account | `bench:ce_install_suite#stage-3-bare-install` |
| Replacing the tunnel from the dashboard of a running server, with the token already in the on-box store, creates a new tunnel, points the wildcard name at it, deletes the old tunnel and moves the connector onto the new credential | `bench:cf_tunnel_regenerate_round_trip#stage-1-rotate-tunnel` |
| A Community server serves one domain: given more, it serves the primary domain, keeps the others stored, and its converge still succeeds | `bench:fi_n10_multidomain_cap` |
| A licensed host serves a second domain through the same tunnel | `bench:ee_multidomain` |
| Of the domains a token reaches, only those chosen in the dashboard are attached | **no bench yet** |
| Changing the domain in the dashboard moves the server onto the new domain: the new domain serves, the old domain's tunnel rule, wildcard name and token are removed, the sign-in page and the applications follow, and a name given to the sign-in page by hand stays | `bench:primary_domain_change_round_trip#stage-3-the-move-is-a-move`, `bench:primary_domain_change_round_trip#stage-4-the-zone-list-names-the-new-domain`, `bench:primary_domain_change_round_trip#stage-4b-the-sign-in-page-names-the-new-domain`, `bench:primary_domain_change_round_trip#stage-5-the-new-domain-serves`, `bench:primary_domain_change_round_trip#stage-6-the-app-follows`, `bench:primary_domain_change_round_trip#stage-7-a-hand-set-sign-in-name-stays` |
| A server joins a self-hosted private-network control server set in the dashboard, in place of the hosted one | `bench:ce_install_headscale` |

### The restic repository -- `backup`

Backups are encrypted on the host and go to an S3 repository set in the
dashboard; restic and rclone ship with the host engines. Restores run on the host
through `catena-recovery`, which the dashboard's restore page drives. On Catena
Business the offsite copy keeps the repository's objects in a locked bucket at
another provider.

| Invariant | Enforced by |
| --- | --- |
| Backups are encrypted on the host before upload | `bench:dr_suite#stage-9-backup-and-pin`, `bench:rebuild_backs_up_after_restore#stage-2-a-password-of-its-own`, `threat:CV4` |
| The backup password is generated on the host once the destination and its keys are saved | `bench:ce_install_suite#stage-4e-configure-backup` |
| The backup password is shown once | **no bench yet** |
| A server rebuilds on a new machine from nothing but the backup destination and the keyset kept off the server | `bench:dr_suite#stage-13-disaster-recovery`, `bench:rebuild_backs_up_after_restore#stage-4-store-holds-the-keysets-password`, `threat:CV9` |
| Saving a destination creates its repository when the bucket is empty, and refuses a bucket holding other files; a backup never creates one | `bench:backup_destination_creates_repository#stage-3-save-creates-the-repository`, `bench:backup_destination_creates_repository#stage-2-save-refused-onto-other-files`, `bench:backup_destination_creates_repository#stage-4-backup-onto-an-emptied-bucket-exits-8` |
| A backup stops when the repository's password is wrong | **no bench yet** |
| A backup that cannot finish, whether the disk is full, the object store is unreachable or its key is refused, fails and never reports success | `bench:fi_d4_disk_full_mid_snapshot`, `bench:fi_n6_s3_endpoint_5xx`, `bench:fi_n7_restic_repo_unreachable`, `bench:fi_s8_s3_hot_revoked` |
| A failed scheduled backup shows as failed on the backup's alert check | `bench:fi_n6_s3_endpoint_5xx`, `bench:fi_s8_s3_hot_revoked` |
| Replacing the backup's object-store keys in the dashboard moves every backup onto the new pair at once, so the old pair can be revoked with no backup failing | `bench:s3_backup_keys_rotation_round_trip#stage-3-backup-on-pair-b`, `bench:s3_backup_keys_rotation_round_trip#stage-5-backup-after-the-revocation` |
| An admin can list, browse and export backups without restoring them | `bench:inplace_restore_suite#wizard-restore-stage-2-page-lists-backups`, `bench:pitr_fuse_round_trip#stage-5-browse-from-the-panel`, `bench:snapshot_export_round_trip#stage-3-extract-canary` |
| Before each snapshot every running Postgres is exported as plain SQL, one file per database server, and a failed export fails the backup | `bench:backup_quarter_hourly#stage-6-only-the-change-is-uploaded`, `bench:fi_b2_pg_dump_failed#stage-3-abort-before-restic` |
| Before each snapshot every running MariaDB is exported as plain SQL, one file per database server, and a failed export fails the backup | **no bench yet** |
| A Postgres built on another image, such as pgvector, PostGIS or TimescaleDB, is exported and replayed like the official one | **no bench yet** |
| Every backup after the first uploads only what changed | `bench:backup_quarter_hourly#stage-6-only-the-change-is-uploaded` |
| Every backup holds the monitoring history of Gatus, Healthchecks and Beszel | `bench:backup_quarter_hourly#stage-6-only-the-change-is-uploaded` |
| Before each backup, the databases of Gatus, Healthchecks and Beszel are copied whole while they run, and a failed copy fails the backup | `bench:ce_restore#stage-2a-the-monitoring-databases-are-copied-whole` |
| The copy of the Gatus, Healthchecks and Beszel databases taken before each snapshot follows no link in the monitoring volumes and keeps no privilege beyond reading them; a planted link fails the backup | **no bench yet** |
| The backup repository compresses what it stores | **no bench yet** |
| A backup waits for each deployed database to answer; a database that never answers is left out, everything else is backed up, and the backup fails naming the application | `bench:backup_quarter_hourly#stage-9-the-backup-waits-for-its-databases` |
| A backup waits up to ten minutes for each deployed database to answer | **no bench yet** |
| Once a day, the first backup after midnight in the server's time zone removes the backups past the retention numbers and checks the repository | `bench:backup_quarter_hourly#stage-8-the-days-first-run-cleans-up` |
| A backup stays listed and restorable until the daily clean-up removes it under the retention numbers | **no bench yet** |
| After the backup destination changes, the System page says "Integrity verified" only once the integrity check has passed on the new destination | `bench:backup_destination_creates_repository#stage-3b-integrity-waits-for-a-check-of-the-new-repository` |
| A restore runs on the host, started from the restore page | `bench:ce_restore#stage-4-ce-restore`, `bench:inplace_restore_suite#wizard-restore-stage-4-restore-through-the-page` |
| The restore page renders in English or French by the browser's language, and offers the same actions in both | `bench:recovery_landing_page_bilingual_parity#stage-1-en-default`, `bench:recovery_landing_page_bilingual_parity#stage-2-fr-variant`, `bench:recovery_landing_page_bilingual_parity#stage-3-action-target-parity` |
| A restore asked for a snapshot the repository does not hold is refused before it changes anything | `bench:fi_b3_snapshot_id_mismatch#stage-1-phony-snapshot-rejected`, `bench:fi_b3_snapshot_id_mismatch#stage-3-nothing-held` |
| A restore that needs more space than the server has free is refused before anything stops, naming the space it lacks | `bench:fi_d3_postgres_oom_mid_restore#stage-1-f-d3-refuses-restore`, `bench:fi_d3_postgres_oom_mid_restore#stage-2-nothing-held` |
| A restore reads this server's repository, another server's or the offsite copy the same way, with a key that may only read, and writes nothing to the offsite copy | `bench:worm_round_trip#stage-6b-restore-from-the-offsite-copy`, `bench:dr_suite#stage-13-disaster-recovery`, `bench:ce_restore#stage-4-ce-restore` |
| A restore writes nothing to this server's own repository | **no bench yet** |
| A whole-server restore puts the data back, adopts the settings stored with it and starts the services Catena runs | `bench:dr_suite#stage-13-disaster-recovery`, `bench:dr_suite#stage-14-post-recovery-assertions`, `bench:dr_suite#stage-19-onbox-store` |
| A whole-server restore starts Gatus, Healthchecks and Beszel on the copies the backup took whole | `bench:ce_restore#stage-4a-the-monitoring-databases-come-back-whole` |
| A whole-server restore joins the private network its settings name | **no bench yet** |
| When a whole-server restore adopts settings whose object-store keys the provider revoked, the restore reads the repository with the keys entered on the restore page, and backups resume on those keys once they are saved with the destination | `bench:post_compromise_rebuild#stage-3-restore-with-pair-b`, `bench:post_compromise_rebuild#stage-3d-destination-confirmed`, `bench:post_compromise_rebuild#stage-4-store-holds-pair-b`, `bench:post_compromise_rebuild#stage-5-backup-on-pair-b` |
| Restored client applications stay stopped until an admin starts them from the restore page, which replays their databases | `bench:dr_suite#stage-14-applications-held`, `bench:dr_suite#stage-15-applications-started`, `bench:dr_suite#stage-15-database-replay-honest`, `bench:inplace_restore_suite#wizard-restore-stage-5b-client-applications-held`, `bench:inplace_restore_suite#wizard-restore-stage-6b-held-applications-started`, `bench:post_compromise_rebuild#stage-3c-applications-started`, `bench:erpnext_helpdesk_restore_round_trip` |
| An application restored from a backup taken in its backup mode comes back as of that backup, its database whole | `bench:actualbudget_backup_mode_round_trip#stage-6-restored-data-opens`, `bench:actualbudget_backup_mode_round_trip#stage-7-started-and-whole`, `bench:mailserver_webmail_backup_mode#stage-6-started-and-whole`, `bench:rocketchat_backup_mode_round_trip#stage-6-started-and-whole` |
| A database replay recreates a Postgres role the dump holds and the database server lost, and gives that role back the objects it owned | `bench:fi_d2_pg_dumpall_replay_constraint#stage-4-replay-recreates-role`, `bench:fi_d2_pg_dumpall_replay_constraint#stage-5-ownership-restored` |
| Starting restored applications takes them out of their backup mode | `bench:inplace_restore_suite#app-restore-stage-4b-backup-mode-released`, `bench:sovereign_exit#stage-2b-held-applications-started-from-a-shell` |
| Starting restored applications from the restore page converges the server | **no bench yet** |
| A one-application restore keeps only that application stopped; a move keeps no application stopped | `bench:inplace_restore_suite#app-restore-stage-4-scope-held`, `bench:inplace_restore_suite#wizard-app-restore-stage-6-scope-held`, `bench:actualbudget_backup_mode_round_trip#stage-5-restored-and-held`, `bench:mailserver_webmail_backup_mode#stage-5-restored-and-held`, `bench:rocketchat_backup_mode_round_trip#stage-5-restored-and-held`, `bench:wizard_migrate_round_trip#stage-5-data-moved-source-recoverable` |
| A one-application restore brings back an application removed from the server, with its data, from a backup that holds it; the restore page lists the applications a backup holds that the server no longer runs | **no bench yet** |
| During a move, the bulk pre-copy starts no application on the new server while the old one keeps serving, and a second pre-copy pass replaces the database archives the first pass left | `bench:migrate_preseed_no_split_brain#stage-3-nothing-started-here`, `bench:migrate_preseed_no_split_brain#stage-4-second-pass-sweeps-old-archives` |
| A move that fails after the old server stopped leaves the old server stopped but reachable, says so and records the failure, never moves the traffic, and never restarts the old server on its own | `bench:wizard_migrate_resume_source#stage-3-no-automatic-resume`, `bench:wizard_migrate_resume_source#stage-5-traffic-never-moved` |
| After a failed move, one command puts the old server back into service, and leaves stopped any application its admin had stopped | `bench:wizard_migrate_resume_source#stage-4-resume-the-source` |
| While a move takes its snapshot, the old server's applications are frozen and its databases keep answering | `bench:wizard_migrate_resume_source#stage-3-no-automatic-resume` |
| After a whole-server restore, backups wait until an admin confirms the destination or saves a new one, and a converge meanwhile succeeds | `bench:dr_suite#stage-14-backups-wait`, `bench:rebuild_backs_up_after_restore#stage-3a-the-backups-wait`, `bench:ce_restore#stage-4.1b-the-backups-wait`, `bench:post_compromise_rebuild#stage-3a-the-backups-wait`, `bench:dr_suite#stage-16-settle-converge` |
| A converge reports backups that wait after a restore as waiting, never as failed | **no bench yet** |
| On a licensed host, a whole-server restore turns the nightly updates and the engine upgrade off, with a warning on every page | `bench:worm_round_trip#stage-6c-backups-wait` |
| After a whole-server restore on a licensed host, the nightly updates and the engine upgrade stay off until an admin turns them back on | **no bench yet** |
| A new backup destination gets a new backup password | `bench:rebuild_backs_up_after_restore#stage-7-a-new-destination`, `bench:worm_round_trip#stage-7c-fresh-repository` |
| An interrupted restore resumes from the same snapshot, and refuses to widen its scope | `bench:fi_n5_provider_outage_mid_restore#stage-2-resume-restore-succeeds`, `bench:inplace_restore_suite#app-restore-stage-6-halted-scope-refuses-to-widen` |
| A restore leaves out, and reports, a database export whose application or database service the backup does not hold, and fails when the database service of an export the backup holds does not start; removing an application removes its database exports | `bench:ce_restore#stage-4.1a-the-export-of-a-removed-application-is-reported`, `bench:fi_n5_provider_outage_mid_restore#stage-2-resume-restore-succeeds` |
| An interrupted restore refuses a different snapshot, is never reported finished, and only a finished run clears its record | **no bench yet** |
| The offsite copy copies the repository's objects into a locked bucket at another provider, and refuses a locked bucket holding a different repository | `bench:worm_round_trip#stage-1-mirror-to-worm`, `bench:offsite_copy_unreachable_target#stage-7-another-repository-refused` |
| The offsite copy reads the repository with source keys of its own: once the backup's old keys are revoked it copies nothing, until its source keys are changed in the dashboard | `bench:s3_backup_keys_rotation_offsite_copy#stage-2-copy-runs-on-pair-a`, `bench:s3_backup_keys_rotation_offsite_copy#stage-4-copy-refused-with-pair-a`, `bench:s3_backup_keys_rotation_offsite_copy#stage-5-copy-follows-pair-b` |
| A bucket repair ends with each application's own consistency check, and reports the files missing from the bucket without failing | `bench:s3_reconcile_orphan_cleanup#stage-2-baseline-report-is-clean`, `bench:s3_reconcile_orphan_cleanup#stage-4-report-names-the-orphan` |
| Each offsite copy first removes the backups past the retention numbers, and while an offsite copy exists that removal never rewrites stored data | `bench:worm_round_trip#stage-0b-dropped-backups-stay-out-of-the-copy`, `bench:rclone_copy_preserves_pruned_packs#stage-5-cold-still-has-pruned-snaps` |
| Recovering an application's bucket copies its objects from the locked bucket into the original bucket, or into a new bucket with new keys that the application then switches to | `bench:worm_round_trip#stage-7a-nextcloud-round-trip` |
| The restore page lists the repositories a locked bucket holds | `bench:worm_round_trip#stage-7c-fresh-repository` |
| A backup repository recovered into a new bucket keeps backing up, and its offsite copy carries on in the same place of the locked bucket; the offsite copy of a fresh repository goes to a new folder of that locked bucket | `bench:worm_round_trip#stage-6c-backup-repository-continues`, `bench:worm_round_trip#stage-7c-fresh-repository` |
| A backup repository is never recovered into the bucket it was copied from, and its copy continues only once the locked bucket proves to hold the same repository | **no bench yet** |
| A restore from a backup brings back the content the backup held, and the applications with it | `bench:backup_rollback`, `bench:ce_restore`, `bench:erpnext_restore_round_trip`, `bench:erpnext_helpdesk_restore_round_trip` |
| The backup repository's password can be replaced with no backup lost | `bench:restic_password_rotation_round_trip` |
| With the dashboard deleted, backups still run, restores still work and the applications stay online | `bench:sovereign_exit`, `bench:recovery_readme_manual_restore` |

### The timers -- `public_ports`, `infrastructure`, `host_maintenance`

A lane is one scheduled job: the backup, the bit-rot check, the restore test,
the offsite copy, the daily chain of nightly maintenance and updates, dashboard updates, the scheduled
converge, the external heartbeat or the vulnerability watch. Every lane ships
with the host engines, each but the vulnerability watch with its timer off;
`catena-schedule apply` turns one on, at the cadence set in the dashboard, when
the host's edition covers it. Jobs that change the server or its backup
repository share one host lock, so they run one at a time. Debian's
unattended-upgrades applies OS packages.

| Invariant | Enforced by |
| --- | --- |
| This repository ships no timer, and every lane ships with the host engines | `audit:check-port`, `threat:CV8` |
| A converged server schedules only one lane, the vulnerability watch | `bench:backup_schedule_applied#stage-1-only-the-watch-is-scheduled-by-default`, `bench:community_free_schedules#stage-1-only-the-watch-is-enabled` |
| A converged server turns on the port reconciler, the dashboard and Gatus syncs, the hourly checks for a needed restart, of the licence and of each application, and the error reports every five minutes; none of them uses object storage | **no bench yet** |
| A lane turns on only on a server whose edition covers it, at the cadence saved in the dashboard | `bench:backup_schedule_applied#stage-3-enable-applies`, `bench:community_free_schedules#stage-2-every-other-lane-refused` |
| A host with no paid edition schedules only the backup, at most weekly, and the vulnerability watch, at most daily, and refuses a faster cadence | `bench:community_free_schedules` |
| On a host with no paid edition, the Schedules page shows each job a paid edition runs with a gray Pro or Business badge and its controls turned off | `bench:community_free_schedules#stage-2b-page-says-what-is-free-and-what-runs-by-hand` |
| A host that loses its paid edition keeps scheduling its backup and its vulnerability watch, at their default time when the saved one is faster | `bench:ee_lapse#stage-3-host-lapsed`, `bench:community_free_schedules#stage-6-a-faster-saved-backup-runs-weekly` |
| A server that loses its paid edition turns off every timer but the backup's and the vulnerability watch's, and keeps the saved schedules | **no bench yet** |
| Every server can run a backup by hand, whatever the backup's schedule | `bench:ce_backup_deferred#stage-2-backup-now-overrides` |
| Every server can run the updates, a converge and a restart by hand | **no bench yet** |
| A dashboard update applies the configuration its new image carries with a converge, and puts the previous dashboard back when that converge fails | `bench:catena_admin_self_update#stage-2-self-update`, `bench:catena_admin_self_update#stage-5-failed-converge-puts-it-back` |
| A release moves each service Catena runs to the version it ships, with its data, and a release whose update fails or whose health falls after the converge puts the previous versions back with their data | `bench:release_moves_dependencies#stage-4-every-service-on-the-release`, `bench:release_moves_dependencies#stage-6-the-failed-release-is-rolled-back`, `bench:release_moves_dependencies#stage-7-a-regression-after-the-converge` |
| The backup lane starts at its set time, with no random delay, and its runs never overlap | `bench:backup_quarter_hourly#stage-3-the-lane-starts-on-the-quarter-hour`, `bench:backup_quarter_hourly#stage-5-the-cadence-holds` |
| Every timer starts at its set time with no random delay, except the licence check, which starts at a minute of its own, fixed per server | **no bench yet** |
| A lane's run still going at the lane's next set time is not started twice, and the lane next starts at the first set time after that run ends | **no bench yet** |
| Each lane's local Healthchecks checks follow the lane's schedule, and the checks of a lane that is off, or that the edition does not cover, are paused | `bench:backup_schedule_applied#stage-3b-checks-follow-the-lane`, `bench:backup_schedule_applied#stage-4b-checks-paused-with-the-lane`, `bench:community_free_schedules#stage-4-lane-checks-follow-the-lanes` |
| The external heartbeat ships off; while on, it calls the outside monitoring address saved in the dashboard, names a monitor that stopped answering, and stops when turned off | `bench:fi_o5_deadman_off_by_default#stage-1-heartbeat-shipped-and-off`, `bench:external_heartbeat_round_trip` |
| The external heartbeat is the one call that leaves the server, and it calls only while the licence is active | **no bench yet** |
| Each application that declares a backup mode is in that mode for the whole nightly backup, and out of it once the backup ends, failed or not | `bench:daily_chain_quiesce_invoked#stage-2-backup-mode-around-the-backup`, `bench:daily_chain_quiesce_invoked#stage-3-backup-fails`, `bench:rocketchat_backup_mode_round_trip#stage-3-night-with-the-backup`, `bench:rocketchat_backup_mode_round_trip#stage-7-failed-backup-releases-the-lock`, `bench:actualbudget_backup_mode_round_trip#stage-3-night-with-the-backup` |
| An application held in its backup mode for minutes keeps its containers, and a sign-in begun during the hold completes once the hold is released | `bench:mailserver_webmail_backup_mode#stage-3-night-with-the-hold`, `bench:mailserver_webmail_backup_mode#stage-4-answers-after-the-release` |
| A nightly run resumed after a restart takes applications out of their backup mode before it backs up again | **no bench yet** |
| Backup-mode commands run inside the application's own container | `bench:app_declarations_travel_with_the_app#stage-3-backup-mode-with-the-catalog-gone`, `bench:app_declarations_travel_with_the_app#stage-6-hand-deployed-stack-gets-its-backup-mode` |
| No backup-mode command runs outside its application's container | **no bench yet** |
| A backup-mode command that runs past its time limit but ends within its grace period has ended before the next command runs | `bench:app_declarations_travel_with_the_app#stage-6b-a-backup-mode-past-its-time-ends-first` |
| An application's command still running at the end of its grace period, which lasts as long as its time limit and two minutes at most, is stopped before the next command runs, and the maintenance log names it with any application left in its backup mode | **no bench yet** |
| An update holds an application's commands to their time: a command stopped before the update refuses it, and a stopped migration rolls it back | **no bench yet** |
| The daily chain applies updates on a server with no backup configured, and the maintenance log says no backup was taken before them | `bench:daily_chain_updates_without_backup#stage-1-no-backup-updates-run` |
| The dashboard warns on every page while no backup is configured | **no bench yet** |
| While backups wait after a restore, the daily chain still applies its updates | **no bench yet** |
| The daily chain runs its steps in order to the end, clears its saved state, and records the night as done | `bench:daily_chain_full_pass#stage-3-chain-runs-to-done`, `bench:daily_chain_full_pass#stage-4-history-entry`, `bench:daily_chain_full_pass#stage-5-status-idle-after-done`, `bench:ee_daily_cycle#stage-3-run-chain`, `bench:ee_daily_cycle#stage-4-status-history` |
| A failed container update does not stop the daily chain: the later steps run and the night's run is recorded as done | `bench:daily_chain_container_rollback#stage-2-chain-reaches-done-despite-container-fail`, `bench:daily_chain_container_rollback#stage-3-state-cleared-on-done` |
| A nightly run of the daily chain with too little free disk stops at its first step, before the backup, and records the disk as low | `bench:daily_chain_preflight_aborts_low_disk#stage-2-chain-aborts-at-preflight`, `bench:daily_chain_preflight_aborts_low_disk#stage-3-state-preserved`, `bench:daily_chain_preflight_aborts_low_disk#stage-4-report-records-disk-low` |
| While the daily chain's lane is on, a run cut short by a restart resumes at boot from its saved state | `bench:daily_chain_resumes_after_reboot#stage-1-lane-on-enables-resume-at-boot`, `bench:daily_chain_resumes_after_reboot#stage-4-chain-resumed-to-done`, `bench:daily_chain_resumes_after_reboot#stage-5-lane-off-unlinks-resume` |
| When the daily chain's saved state is damaged, the next run starts from the first step, and the log shows what was found | `bench:daily_state_corrupt_fallback#stage-2-corrupted-state-starts-new-night` |
| The daily chain stops before any update while a service the monitoring probes is failing, and names that service | `bench:ee_daily_cycle#stage-5-gate-stops-on-a-red-endpoint` |
| On a server that never chose the daily chain's steps, the chain runs every step but the vulnerability watch and the restore test; each night, the chain runs only the steps kept on | `bench:daily_chain_step_list#stage-1-defaults-rendered-en-fr`, `bench:daily_chain_step_list#stage-3-night-runs-only-the-chosen-steps` |
| The restore test and the vulnerability watch each run on their own lane or as a step of the daily chain, never both | `bench:daily_chain_step_list#stage-9b-restore-test-step-or-lane`, `bench:daily_chain_step_list#stage-9-cve-watch-step-or-lane` |
| The restore test restores the latest backup into a scratch area and checks it | **no bench yet** |
| Weekly, the restore test starts the restored databases and each application that declares a start check, and checks that each opens | `bench:verify_hot_bootprobe_weekly#stage-3-forced-bootprobe-runs`, `bench:erpnext_restore_round_trip#stage-16-boot-probe-lists-erpnexts-database` |
| The weekly restore test starts the restored databases and applications on a network of their own with no published port | **no bench yet** |
| The offsite copy runs only after the retention removal and a passing repository check and, when the option is on, after a passing restore test of the newest backup | `bench:offsite_copy_refuses_an_unchecked_backup#stage-2-repository-check-refuses`, `bench:offsite_copy_refuses_an_unchecked_backup#stage-4-option-on-refusals`, `bench:worm_round_trip#stage-0b-dropped-backups-stay-out-of-the-copy` |
| Leaving the backup, its repository check, the restore test or the health check before the updates out of the daily chain is allowed with a warning, and updates without the backup step bring a warning on every page | `bench:daily_chain_step_list#stage-5-updates-without-backup-warn`, `bench:daily_chain_step_list#stage-6-health-gate-left-out-warns`, `bench:daily_chain_step_list#stage-8-backup-without-its-check-warns`, `bench:daily_chain_step_list#stage-9a-untested-backup-means-no-restore-test-scheduled` |
| Leaving the offsite check out of the daily chain is allowed with a warning | **no bench yet** |
| A failed offsite check stops the daily chain before any update when the server requires a good offsite copy for updates, and lets the updates run when it does not | `bench:daily_chain_verify_cold_fail_configurable#stage-2-require-true-aborts`, `bench:daily_chain_verify_cold_fail_configurable#stage-3-state-preserved-on-abort`, `bench:daily_chain_verify_cold_fail_configurable#stage-4-require-false-continues`, `bench:daily_chain_verify_cold_blocks_mirror#stage-2-chain-aborts-on-cold-fail` |
| While the last offsite check has failed, the offsite copy of the backup repository refuses to run, the copy of an application's bucket still runs, and the refusal ends once the check passes | `bench:daily_chain_verify_cold_blocks_mirror#stage-3-sentinel-written-fail`, `bench:daily_chain_verify_cold_blocks_mirror#stage-4-repository-copy-refuses`, `bench:daily_chain_verify_cold_blocks_mirror#stage-5-unrelated-copy-not-gated`, `bench:daily_chain_verify_cold_blocks_mirror#stage-6-sentinel-cleared-on-recovery` |
| A failed restore test or repository check stops the daily chain before any update | `bench:daily_chain_verify_hot_fail_aborts_updates#stage-4-update-tail-skipped`, `bench:restic_check_subset_weekly#stage-8-night-stops-before-the-updates` |
| When the nightly backup fails, the daily chain's run ends there, recorded as failed at the backup step | `bench:rocketchat_backup_mode_round_trip#stage-7-failed-backup-releases-the-lock`, `bench:daily_chain_quiesce_invoked#stage-3-backup-fails` |
| A failed backup stops the daily chain before any update, unless the backup step is left out of the chain | **no bench yet** |
| The engine upgrade moves the container engine to the version pinned here, and puts the previous one back when the new one does not come up | `bench:docker_engine_upgrade_round_trip#stage-2-a-new-engine-that-does-not-start-goes-back`, `bench:docker_engine_upgrade_round_trip#stage-3-the-new-engine-holds` |
| Updating Catena from the control panel moves Docker to the version the new release pins | `bench:update_from_previous_release#stage-3a-the-engine-runs-the-release-pin` |
| In the daily chain, the engine upgrade runs after the container updates | `bench:ee_daily_cycle#stage-3-run-chain` |
| In the daily chain, the engine upgrade stays within the major version the engine runs | **no bench yet** |
| The daily chain's last step restarts the server when an update needs one, also after a night that stopped before any change, and checks that every service came back | `bench:daily_chain_reboot_round_trip#stage-3-the-chain-restarts-the-host`, `bench:daily_chain_reboot_round_trip#stage-4-everything-came-back`, `bench:daily_chain_step_list#stage-14-stopped-night-still-restarts` |
| The daily chain reports to its own alert check and records its result, or the step that failed, in the maintenance log | `bench:daily_chain_step_list#stage-3-night-runs-only-the-chosen-steps`, `bench:daily_chain_step_list#stage-10-failed-step-recorded`, `bench:daily_umbrella_healthchecks#stage-1-success-path-pings-start-and-succeeded`, `bench:daily_umbrella_healthchecks#stage-2-abort-path-pings-fail`, `bench:nightly_update_real#stage-3-after-the-night` |
| Jobs that change the server or its backup repository take the host lock one at a time; a scheduled job waits, and a dashboard change meanwhile answers busy | `bench:concurrent_backup_lock_contention#stage-1-units-take-the-host-lock`, `bench:concurrent_backup_lock_contention#stage-5-settings-answers-busy`, `bench:daily_chain_step_list#stage-13-night-waits-then-holds-the-lock`, `bench:ee_daily_cycle#stage-6-night-lends-the-host-lock` |
| A nightly run of the daily chain that cannot take the host lock in time is skipped and reported, and the chain runs again the next night | `bench:daily_chain_step_list#stage-12-night-skipped-while-the-lock-is-held` |
| A nightly run of the daily chain waits up to an hour for the host lock | **no bench yet** |
| The vulnerability watch runs on every host with no licence, against a fresh vulnerability database and the list of vulnerabilities known to be exploited | `bench:community_security_alerts#stage-2-the-watch-runs-with-no-licence`, `bench:cve_watch_emergency_patch#stage-3-database-downloaded-into-place` |
| On every edition, a vulnerability known to be exploited raises one alert, whether or not a fixed version exists; the alert names each application, its image, the installed package version and the version that fixes it | `bench:community_security_alerts#stage-3-a-fixable-vulnerability-alerts-once`, `bench:community_security_alerts#stage-4-an-exploited-one-with-no-fix-alerts-once`, `bench:community_security_alerts#stage-5-no-second-alert` |
| The maintenance log records each alert with the application, its image and the version that fixes it | `bench:community_security_alerts#stage-3-a-fixable-vulnerability-alerts-once` |
| A vulnerability alert is not counted as a failed job or a service down | `bench:community_security_alerts#stage-3b-an-alert-is-no-failed-job` |
| The vulnerability watch alerts for a critical vulnerability published in the last 30 days that has a fixed version | **no bench yet** |
| The vulnerability scan writes a well-formed report, also when it finds nothing | `bench:cve_residual_emits_findings#stage-2-cve-scan-shape`, `bench:cve_residual_emits_findings#stage-4-rerun-preserves-shape` |
| Every edition shows the vulnerabilities found in each application's images, with the package, its installed version and the version that fixes it; the emergency patch, running the watch on demand and the software inventory need a paid edition | `bench:community_security_alerts#stage-8-the-findings-show-and-the-patch-stays-pro`, `bench:cve_watch_emergency_patch#stage-16-lapsed-licence-refuses` |
| The emergency patch's automatic mode needs a paid edition | **no bench yet** |
| An emergency patch moves a service to the oldest release its update policy allows that a scan on the host shows free of the vulnerability, and rolls back when health falls | `bench:cve_watch_emergency_patch#stage-13-patched-to-oldest-free-release`, `bench:cve_watch_emergency_patch#stage-12-regression-rolls-back`, `threat:CV16` |
| The emergency patch's automatic mode patches only an exploited vulnerability, never a service kept off the updates, and never while updates are paused | `bench:cve_watch_emergency_patch#stage-14-automatic-moves-only-what-it-may` |
| An emergency patch runs behind the update's own copies | **no bench yet** |
| unattended-upgrades applies OS packages on every host, licensed or not, from the security suite, stable updates, point releases and the private-network client's own repository | `bench:ce_validate#stage-1-ce-validate`, `bench:reconcile_repairs_host_upkeep#stage-3-restored` |
| A service left running on a replaced library restarts after the update; the container engine waits for a restart of the host | `bench:reboot_required_notified#stage-6-replaced-libraries` |
| A host that needs a restart shows it on the dashboard until it is taken, and without a licence waits for a person | `bench:reboot_required_notified#stage-3-pending-reboot-reported`, `bench:reboot_required_notified#stage-5-host-was-not-rebooted` |

### The release manifest -- written last, by both converge paths

Every converge, from `converge.yml` or `reconcile.yml`, records what it delivered,
after removing what it no longer ships. `/var/lib/catena/payload-manifest.json`
lists the files the host engines claim.

| Invariant | Enforced by |
| --- | --- |
| Every converge records what it delivered | `bench:converge_suite#stage-1-manifest-describes-this-converge`, `bench:converge_suite#stage-7a-the-action-is-back`, `bench:reconcile_is_a_noop#stage-1-first-reconcile` |
| A converge removes a file that an earlier converge installed and the current one no longer ships, unless the file was changed since, in which case it stays | `bench:install_rerun#stage-4b-withdrawn-pruned-drift-reported`, `bench:payload_prune_respects_ce#stage-2-withdrawn-removed` |
| A converge never removes a Community file as though it were a licensed one, and never removes a file the host engines claim | `bench:payload_prune_respects_ce#stage-1-coexistence`, `bench:payload_prune_respects_ce#stage-3-foreign-copy-survives`, `workflow:ci.yml#installer` |

### The version stamp -- written first, by both converge paths

`/etc/catena/version.txt` names, as `git describe`, the version of this repository
a converge applied, from the provenance the release image records beside its
tree. Every backup holds it.

| Invariant | Enforced by |
| --- | --- |
| `converge.yml` and `reconcile.yml` each stamp the version of the image whose tree they ran | `bench:reconcile_is_a_noop#stage-1-first-reconcile`, `bench:update_from_previous_release#stage-3-one-version-after-the-update` |
| Every snapshot names the version that produced its data | `bench:recovery_readme_manual_restore#stage-2-readable-from-snapshot` |
| The version stamp is the first thing a converge writes; a tree with no provenance writes no stamp and leaves the previous one | **no bench yet** |
| A restore refuses a newer snapshot before it changes anything, and restores an older one only when the upgrade is asked for | `bench:inplace_restore_suite#skew-abort-stage-2-restore-refuses`, `bench:inplace_restore_suite#skew-abort-stage-3-zero-side-effects`, `bench:inplace_restore_suite#skew-upgrade-stage-2-refused-without-the-flag`, `bench:inplace_restore_suite#skew-upgrade-stage-4-upgrade-permits-this-direction` |
| A restore refuses a backup whose version stamp cannot be ordered against the server's | **no bench yet** |
| A restore asked to upgrade runs each restored application's declared migration once its data is back, never stops a migration part-way, waits at least half an hour, and reports a migration still running then | **no bench yet** |
| A move between two servers is refused unless both run the same version | **no bench yet** |

## Applications

The dashboard deploys applications from a catalogue resolved per host. Each
application lives in its own folder, `/srv/catena/compose/<stack>`, with its
compose file, its `.env` and a record of where it came from, and the host's
`catena-apps` deploys it on Docker Swarm. The `x-catena` block of its compose
file declares its settings and its commands. Per-app compose files and wiring
live in `catena-templates`. Shared services answer on `auth`, `dash`, `gatus`,
`healthchecks` and `beszel` under the domain. Each sign-in entry admits one
tier, administrators or staff, or every account when the application's labels
open it to clients; an entry also admits the members of the access groups its
application declares. Catena's sign-on extension
(`ansible/reconcile/roles/keycloak/provider`) refuses everybody else.

| Invariant | Enforced by |
| --- | --- |
| Each catalogue application is routed on its own address | `bench:catalog_apps_read_green_in_gatus#stage-1-routed` |
| An application can publish several addresses, each routed, watched and gated on its own; an address another application holds, or one of Catena's, is refused, and an application's own proxy labels route nothing | `bench:app_several_addresses#stage-1-a-route-per-address`, `bench:app_several_addresses#stage-3-access-per-address`, `bench:app_several_addresses#stage-4-a-cookie-opens-its-own-address-alone`, `bench:app_several_addresses#stage-5-a-status-card-per-address`, `bench:app_several_addresses#stage-6-a-taken-host-is-refused`, `bench:app_several_addresses#stage-9-catena-addresses-refused`, `bench:app_several_addresses#stage-10-traefik-labels-route-nothing` |
| An application that asks for single sign-on gets its own sign-in entry, kept in step with its labels and removed with the application; entries made by hand are left alone | `bench:oidc_label_client_provisioned`, `bench:mailserver_after_domain#stage-3-wired-without-a-press`, `threat:CV13` |
| Catalogue applications that offer single sign-on in their free edition sign in with the suite's account | `bench:catalog_app_sign_in` |
| An account outside a sign-in entry's tier and its access groups is refused on the sign-on page, also when it signs in through an external identity provider, and a refusal for the tier does not count as a failed sign-in | `bench:catalog_app_sign_in#stage-client-tier-refused`, `bench:catalog_app_sign_in#stage-broker-tier-check`, `bench:catalog_app_sign_in#stage-access-refused-without-it`, `workflow:ci.yml#keycloak-provider`, `threat:CV2` |
| Every external identity provider, an organization's included, runs the tier check from the moment it is added, and a converge leaves the sign-in flows as they are | `bench:catalog_app_sign_in#stage-broker-before-converge`, `bench:catalog_app_sign_in#stage-broker-organization-provider`, `bench:catalog_app_sign_in#stage-broker-converge-keeps-the-flows`, `workflow:ci.yml#keycloak-provider` |
| A client may sign in with a code emailed to the address they type, in place of the password, while outgoing mail is set; every address gets the same answer, staff, administrators, disabled and unknown accounts are sent no code and cannot use one, a code that does not sign in counts toward the lockout, and the code never creates an account | `bench:catalog_app_sign_in#stage-client-email-code`, `workflow:ci.yml#keycloak-provider` |
| Giving a client access to an application opens its sign-in to them and gives them the application's role before their first sign-in; removing that access refuses their next sign-in and takes the role back | `bench:catalog_app_sign_in#stage-access-given-signs-in`, `bench:catalog_app_sign_in#stage-access-removed-refused` |
| A changed sign-in flow is built whole under a new name before the sign-in realm switches to it, so a failed converge leaves sign-in working on the previous version; a flow edited by hand is replaced by a new version, and versions no longer in use are removed | `workflow:ci.yml#installer` |
| Each application gets a password generated on the host | **no bench yet** |
| An application's hooks configure it after every deploy, and a further deploy changes nothing | `bench:suite_wiring_commands#stage-talk-hpb-wired`, `bench:wordpress_install_and_mail#stage-6-a-converge-changes-nothing`, `bench:mailserver_after_domain#stage-3-wired-without-a-press`, `bench:nextcloud_versions_retention_applied#stage-1-occ-reads-retention`, `bench:nextcloud_versions_retention_applied#stage-2-occ-reads-trash-retention` |
| A certificate an application declares is issued by the host into its volume and loaded, and the scheduled renewal replaces a due one and has the application serve it | `bench:fi_c7_coturn_cert_expired#stage-1-renewal-job-renews`, `bench:mailserver_after_domain#stage-3-wired-without-a-press`, `bench:app_declared_needs#stage-1-needs-met-before-the-deploy` |
| An application's hooks configure it after every update and every restore | **no bench yet** |
| An application that reads a value another application provides is deployed again with that value once the provider starts | `bench:host_network_app_ports#stage-5-reader-redeployed` |
| Antivirus and office editing reach the applications that declare them | **no bench yet** |
| An application deploys from its folder, which holds its compose file, its `.env` and its origin | `bench:apps_folder_round_trip#stage-1-private-repository-needs-its-token` |
| Each application's folder is owned by root and readable by root alone | **no bench yet** |
| An application setting reaches the application exactly as typed, `$(...)` and backticks included, and runs nothing | `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed` |
| An application setting holding a line break is refused, and the setting keeps its previous value | `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed` |
| `catena-apps` runs `docker stack deploy` as root with the `.env` values as the only environment, and refuses the keys that steer the Docker client or the loader (`DOCKER_*`, `PATH`, `HOME`, `LD_*`, the proxy variables) | **no bench yet** |
| The app checker runs before every deploy, and a file it rejects is not deployed | `bench:app_checker_catalog#stage-4-deployed-file-counted-on-its-tile`, `bench:host_network_app_ports#stage-6-hand-copy-refused` |
| A compose file that does not load is rejected before it reaches a host | `bench:fi_u1_compose_lint_reject` |
| An application whose compose file was edited into a broken state is repaired in place from its published source | `bench:repair_broken_template_round_trip` |
| An application gets the host network only when its compose file is an unchanged catalogue file that declares it | `bench:host_network_app_ports#stage-2-ports-open`, `bench:host_network_app_ports#stage-6-hand-copy-refused` |
| An application gets the container engine's socket only when its compose file is an unchanged catalogue file that declares it | **no bench yet** |
| Each backup holds the application folders, and a restore brings them back | `bench:app_declarations_travel_with_the_app#stage-5-restore-brings-the-block-back`, `bench:inplace_restore_suite#app-restore-stage-5-definition-rolled-back` |
| An application keeps the backup mode, update steps and restore steps it was deployed with; the server needs no catalogue to back it up, update it or restore it | `bench:app_declarations_travel_with_the_app` |
| An application is added from a repository on the server's list, a private one only with its token, which is never shown | `bench:app_origin_update#stage-1-added-on-release-one`, `bench:apps_folder_round_trip#stage-1-private-repository-needs-its-token` |
| An application added from a repository stays stopped, with its settings and generated values written, until a person starts it, and the settings saved before its first start are in its first deploy | `bench:apps_folder_round_trip#stage-1-private-repository-needs-its-token`, `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed` |
| The server's list of application repositories holds Catena's catalogue until an admin changes it | **no bench yet** |
| The host fetches a repository with git as `catena-git` over https only; root reads the result as data, refuses links, folders outside the application's folder and oversized folders, and keeps the token where only root reads it | **no bench yet** |
| An application's settings come from values a person gives and from facts the server holds | `bench:apps_folder_round_trip#stage-2-settings-arrive-as-typed`, `bench:app_declared_needs#stage-1-needs-met-before-the-deploy`, `bench:oidc_label_client_provisioned#stage-1-entry-created`, `bench:mailserver_after_domain#stage-6-relay-password-reaches-the-mail-server`, `bench:mailserver_after_domain#stage-7-spamhaus-key-reaches-rspamd` |
| A secret of the on-box store reaches an application only when its block lists it, only for an unchanged catalogue file, and never one that opens the host (the backup, Cloudflare, licence and admin credentials) | **no bench yet** |
| A declared verify command answers for its application | `bench:host_network_app_ports#stage-3-verify-hook-ok` |
| Each application's verify command runs hourly, and a failure is named once in the maintenance log | **no bench yet** |
| A verify asked while an application is starting answers that it is starting, the dashboard says so, and once the start time its `x-catena` block allows passes without an answer the verify fails and the maintenance log names it | `bench:apps_folder_round_trip#stage-6-starting-until-ready` |
| An application changed by hand, or holding a person's own version of its file, is not updated, and the dashboard says so | `bench:apps_folder_round_trip#stage-4-a-changed-folder-is-held`, `bench:app_origin_update#stage-5-changed-folder-is-held` |
| A stopped application, or one changed through another tool, is left alone by the daily chain | **no bench yet** |
| An update moves every service of an application that shares an image together, runs its declared migration once the application answers, and keeps the new version | `bench:nextcloud_update_migrates#stage-4-updated-and-migrated`, `bench:erpnext_helpdesk_update_migrates`, `bench:erpnext_update_migrates` |
| The nightly update moves an application to a newer build its policy allows, and the maintenance log names it | `bench:nightly_update_real#stage-3-after-the-night` |
| An application pinned on a nightly build moves onto its newest stable release under the major policy, all its images in one deploy | `bench:tag_scheme_migration#stage-3-moved-together` |
| A failed update of an application or of a service Catena runs puts the previous version and its data back by itself | `bench:nextcloud_failed_migrate_rolls_back`, `bench:erpnext_failed_migrate_rolls_back`, `bench:espocrm_custom_survives_update#stage-5-rolled-back`, `bench:infra_stack_update_rollback#stage-3-auto-recovery`, `bench:infra_stack_update_rollback#stage-4-keycloak-rolls-back-with-its-data`, `bench:infra_stack_update_rollback#stage-7-a-connector-that-never-connects`, `bench:control_plane_update_rollback#stage-3-auto-recovery` |
| A rolled-back update is recorded with its version, which is quarantined once and kept out of every later update | `bench:fi_u5_persistent_quarantine#stage-3-durable-quarantine`, `bench:fi_u6_full_rollback_state#stage-3-state-records-rollback`, `bench:control_plane_update_rollback#stage-3-auto-recovery` |
| A good update of a service Catena runs keeps its data, and the next converge leaves it on the new version | `bench:infra_stack_update_rollback#stage-5-keycloak-keeps-a-good-bump` |
| What a person built inside an application survives an update, and a rollback | `bench:espocrm_custom_survives_update#stage-5-rolled-back`, `bench:espocrm_custom_survives_update#stage-8-moved-and-intact` |
| A service its template keeps off the automatic updates moves only when an admin of a paid server confirms the version the nightly check names, keeps its data and its exemption, goes back with its data when health falls, and is neither offered nor moved without a licence | `bench:pinned_service_update#stage-2-nightly-names-the-newest`, `bench:pinned_service_update#stage-4-moved`, `bench:pinned_service_update#stage-7-lapsed`, `bench:pinned_service_update#stage-9-rolled-back` |
| An application's writes are held while its database moves to a new major version, and an admin can revert it to the previous files and data | `bench:app_database_upgrade` |
| Sync all, and every converge the dashboard or a schedule starts, let the dashboard sync they start run under their host lock, so the sync writes sign-in values, mailboxes and application states at once | `bench:oidc_label_client_provisioned#stage-3-secret-change-followed`, `bench:mailbox_sync_ownership#stage-3-sync-adds-and-records`, `bench:apps_folder_round_trip#stage-4-a-changed-folder-is-held`, `bench:app_origin_update#stage-5-changed-folder-is-held` |
| A revert, a failed update's rollback and the recovery of an interrupted update put the copied data back before the previous version starts, and a deploy that does not end within ten minutes fails and releases the host lock | `bench:app_database_upgrade#stage-3-revert-puts-the-files-and-data-back` |
| When Stop or Delete says it is done, the application's containers and network are already gone, so Start or Add works right away | `bench:apps_folder_round_trip#stage-3-stop-and-start-keep-the-data`, `bench:apps_folder_round_trip#stage-5-delete-erases-the-data`, `bench:suite_wiring_commands` |
| When a monitor stops answering, the external heartbeat names it to the outside monitoring service | `bench:external_heartbeat_round_trip#stage-3-a-silent-monitor-is-named` |
| Gatus probes each shared service, Beszel's hub included | `bench:gatus_probes_beszel_hub` |
| The single sign-on signing keys, the dashboard's session-cookie secret and the sign-in gate's client secret can be replaced without breaking sign-in | `bench:keycloak_signing_keys_rotation_round_trip`, `bench:oauth2_proxy_cookie_rotation_round_trip`, `bench:fi_a2_oidc_secret_rotation#stage-1-rotate-secret-reconverges`, `bench:fi_a2_oidc_secret_rotation#stage-2-keycloak-client-secret-rotated` |
| Every admin page and admin tool sits behind a sign-in gate, and the dashboard refuses a staff member every admin page and action | `bench:app_container_cannot_claim_an_identity`, `bench:gatus_probes_beszel_hub#stage-1-hub-sends-anonymous-to-sign-in`, `bench:fi_a5_wrong_group_assignment#stage-2-admin-host-rejects-staff`, `bench:fi_a5_wrong_group_assignment#stage-3-staff-host-allows-staff`, `bench:admin_staff_cannot_reach_admin_routes#stage-1-admin-pages-refuse-staff`, `bench:admin_staff_cannot_reach_admin_routes#stage-2-admin-actions-refuse-staff`, `threat:CV2` |
| An application is deleted only while stopped | `bench:apps_folder_round_trip#stage-7-delete-needs-a-stopped-app` |
| Before deleting an application, the control panel names the date of the latest backup that holds it; when no backup holds it, the application's name must be typed in | `bench:apps_folder_round_trip#stage-5-delete-erases-the-data` |
| An application's card shows the logo its package ships only after the server strips anything in it that could run a script or load an outside address, and serves it from the control panel's own address | `bench:apps_folder_round_trip#stage-9-a-package-logo-is-cleaned-and-shown` |
| A generated password that an application's package marks for a person's sign-in is shown to an administrator only when asked, and each look is recorded without the password | `bench:apps_folder_round_trip#stage-10-a-generated-password-shows-on-request` |
| A setting the application's package marks for the first start only can be changed until the application first starts, and is refused after | `bench:apps_folder_round_trip#stage-8-a-first-start-setting-is-locked-after-the-start` |
| A staff member's application tiles carry no badge | `bench:admin_apps_tab_staff_filtered#stage-1-staff-tiles` |
| The Apps tab shows a staff member only the tiles their groups open and no admin tab, and an admin every tile and tab | `bench:admin_apps_tab_admin_full#stage-1-every-tile`, `bench:admin_apps_tab_admin_full#stage-2-every-tab`, `bench:admin_apps_tab_staff_filtered#stage-1-staff-tiles`, `bench:admin_apps_tab_staff_filtered#stage-2-no-admin-tab` |
| Groups made in the dashboard are honoured by the sign-in gates; a destructive group change applies only once the impact shown is confirmed, a group change sent from another subdomain of the domain is refused, and the sign-in tiers cannot be renamed | `bench:identity_group_gates_access#stage-4-stale-confirmation-refused`, `bench:identity_group_gates_access#stage-5-a-sibling-subdomain-cannot-post`, `bench:identity_group_gates_access#stage-6-rename-through-the-edge`, `bench:identity_group_gates_access#stage-9-sign-in-tiers-keep-their-names`, `bench:identity_group_gates_access#stage-10-matching-confirmation-applies` |
| A person gets an account only by invitation from the dashboard: the account is made in the group chosen, with the application access chosen (for clients only), and the person confirms the address and sets their own password from the email | `bench:identity_group_gates_access#stage-11-invitation-makes-the-account`, `bench:identity_group_gates_access#stage-12-password-link-sent-again` |
| Access groups keep the names Catena gives them, and a staff or administrator account never holds one | `bench:identity_group_gates_access#stage-13-access-groups-are-reserved`, `bench:identity_group_gates_access#stage-11-invitation-makes-the-account` |
| With outgoing mail off no invitation is made, and an invitation whose email cannot be sent leaves no account | **no bench yet** |
| Changing the admin email moves the admin's accounts in the sign-on service and in the monitors to the new address, and keeps the password | `bench:admin_email_change_follows#stage-2-one-administrator-moved`, `bench:admin_email_change_follows#stage-3-keeps-its-password`, `bench:admin_email_change_follows#stage-4-monitors-follow` |
| The sign-on service's own admin account locks out repeated wrong passwords with a growing pause and is asked to set up a one-time code; a converge keeps both, and the documented server commands lift a lockout or a lost code | `bench:keycloak_master_realm_hardening#stage-1-master-realm-posture`, `bench:keycloak_master_realm_hardening#stage-2-wrong-passwords-lock-with-a-growing-pause`, `bench:keycloak_master_realm_hardening#stage-3-lockout-lifted-from-the-server`, `bench:keycloak_master_realm_hardening#stage-5-next-sign-in-asks-for-the-code`, `bench:keycloak_master_realm_hardening#stage-6-converge-through-the-automation-client`, `bench:keycloak_master_realm_hardening#stage-7-lost-code-asked-again` |
| An admin who lost access to their mail and their second factor creates a new, named sign-on admin from the server | `bench:keycloak_admin_email_loss_recovery#stage-2-recovery-admin-authenticates`, `bench:keycloak_admin_email_loss_recovery#stage-3-second-factor-asked-when-required` |
| A user who lost their password or second factor is reset from the server and sets a new one at the next sign-in | `bench:user_recovery_kcadm_temp_password`, `bench:user_recovery_2fa_reset` |
| A client application's container cannot send forged network frames to a Catena service on the network they share, and the app checker reports a file keeping the raw-packet capability | `bench:client_container_raw_network#stage-1-no-link-layer-forgery-on-the-shared-network`, `bench:client_container_raw_network#stage-2-hand-deployed-file-is-flagged` |
| A sign-in cookie a client application or another admin tool sees opens no admin tool | `bench:app_container_cannot_claim_an_identity`, `threat:CV2` |
| No sign-in gate shows its credentials in its service definition | `bench:oauth2_proxy_cookie_rotation_round_trip`, `threat:CV7` |
| The dashboard runs a newly shipped action with no converge first, and refuses an action it does not know | `bench:payload_action_dispatches_without_converge#stage-3-new-name-no-converge`, `bench:admin_action_unknown_rejected`, `bench:ce_admin_actions` |
| The dashboard loads every script, style sheet and icon from its own address | `bench:ce_admin_smoke#stage-8-own-origin-assets` |
| The record of administrative actions can be exported and checked away from the server: an edited or shortened record fails the check | `bench:audit_chain_tamper_evident`, `threat:CV10` |
| Each application's user sync reaches the identity provider through the dashboard on the host's loopback, and keeps running while the tunnel is down | `bench:mailbox_sync_ownership#stage-5-sync-without-the-tunnel` |
| Application sign-in provisioning keeps running while the tunnel is down | **no bench yet** |
| The dashboard forwards to the identity provider only the calls that sync makes, behind the catalogue's per-host token, and adds no credential of its own | **no bench yet** |
| Settings > Outgoing mail sets the sign-in realm's mail while the realm holds what Catena wrote; settings changed in the realm's own Email tab survive a converge and a change of the outgoing mail | `bench:ce_install_suite#stage-4h1-realm-mail-from-the-store`, `bench:realm_hand_edits_survive_converge#stage-2f-mail-save-keeps-the-email-tab` |
| Alerts reach the admin email through the outgoing mail | `bench:ce_install_suite#stage-4h2-alerts-reach-the-admin-by-email` |
| With outgoing mail off, alerts reach only the channels added in Healthchecks | **no bench yet** |
| A shared service's subdomain is a dashboard setting that uses the default name until another is saved; changing it moves every route of the service to the new name | `bench:infra_subdomain_change#stage-0b-an-untouched-save-stores-no-default`, `bench:infra_subdomain_change#stage-1-settings-takes-the-new-name`, `bench:panel_renames_itself#stage-2-change-the-name-through-the-panel`, `bench:panel_renames_itself#stage-4-converge-onto-the-new-name`, `bench:panel_renames_itself#stage-5-the-new-address-answers`, `bench:panel_renames_itself#stage-6-the-old-name-routes-nowhere` |
| A new dashboard address saved in Settings is named on the page before the dashboard moves, and none is named while nothing moves | `bench:panel_renames_itself#stage-1-nothing-is-announced-while-nothing-moves`, `bench:panel_renames_itself#stage-3-the-page-names-where-it-is-going` |
| Admins see the version each application and service runs | `bench:versions_shown_to_administrators#stage-3-system-tab-names-the-versions` |
| The System tab shows the server's own processor readings and links out to the resource monitor | `bench:catena_admin_resources_tab_links_beszel#stage-1-resources-redirects-into-system`, `bench:catena_admin_resources_tab_links_beszel#stage-2-system-links-the-monitor`, `bench:catena_admin_resources_tab_links_beszel#stage-3-system-shows-this-servers-readings` |
| Gatus names each service without its version, so its history survives updates | **no bench yet** |
| The installer's administrator opens the sign-on realm's own admin console; a role removed by hand stays removed | `bench:keycloak_master_realm_hardening#stage-8-realm-admin-opens-the-realm-console` |
| The resource monitor opens signed in for an administrator who passed its sign-in gate, refuses a call from another site, and keeps its own password sign-in for when the sign-on service is down | `bench:gatus_probes_beszel_hub#stage-6-the-gate-signs-the-admin-in`, `bench:gatus_probes_beszel_hub#stage-7-a-forged-email-signs-no-one-in`, `bench:gatus_probes_beszel_hub#stage-8-a-cross-site-call-is-refused`, `threat:CV2` |
| Each Settings section shows whether its last check passed after its last save; a value changed outside the dashboard clears that result | **no bench yet** |
| Outgoing mail sends a test message to the admin address on request | **no bench yet** |
| With Catena Business, each change made through the panel appears in the Log tab with who made it and whether it was refused | **no bench yet** |

## Paid features

How the dashboard features a paid edition unlocks behave. Which edition
unlocks which is on the website's
[edition comparison](https://catena.run/en/#pricing).

| Invariant | Enforced by |
| --- | --- |
| Each licensed maintenance button dispatches its own action | `bench:ee_named_buttons#stage-1-audit-export`, `bench:ee_named_buttons#stage-3-verify-backup`, `bench:ee_named_buttons#stage-4-daily-run-now` |
| Each administrative action reaches the central collector intact, and is in the host's own hash chain | `bench:ee_audit_ship#stage-3-loki-assert`, `bench:ee_audit_ship#stage-4-dual-sink` |
| The monthly compliance attestation is signed from the host's own evidence, carries its audit chain's head and key, verifies against the published key, and a tampered one is refused | `bench:ee_attest#stage-1.5-host-audit-digest`, `bench:ee_attest#stage-2-sign`, `bench:ee_attest#stage-3-verify` |
| The compliance panel lists every control of its registry in English and French and filters them by framework; its attestations form a hash chain that verifies, it refuses an attestation of a control Catena measures and a post from another site, and its records survive a redeploy, are in every backup and export | `bench:ee_compliance_panel#stage-2-panel-renders-en-fr`, `bench:ee_compliance_panel#stage-3-framework-filter`, `bench:ee_compliance_panel#stage-5-attest-manual-control`, `bench:ee_compliance_panel#stage-8-cross-origin-refused`, `bench:ee_compliance_panel#stage-9-persists-across-redeploy`, `bench:ee_compliance_panel#stage-10-covered-by-backup`, `bench:ee_compliance_panel#stage-11-export-carries-the-chain` |
| The monthly report shows, in English and French, the month's backups, restore tests, updates, vulnerabilities, availability and alerts from the server's own records, and names what the server does not record | `bench:monthly_report_renders#stage-3-the-report` |
| The identity posture probe reports unhealthy while multi-factor sign-in is off, and healthy once the dashboard turns it on | `bench:ee_identity_probe#stage-2-drift`, `bench:ee_identity_probe#stage-3-healthy` |

## CI

`ci.yml` runs the Ansible tree's and the installer's unit tests, the entry
point and its wheel, the sign-on extension's build, Ansible syntax, and the
copy-paste, sentence and prose ratchets. `security.yml` runs scanctl.
`publish-installer.yml` publishes the installer to PyPI from a release tag.
Pinned images move through catena-admin's nightly engine bump; every other
dependency follows [renovate](https://github.com/catenahq/renovate) and
[renovate-config](https://github.com/catenahq/renovate-config), and
[scanctl](https://github.com/catenahq/scanctl) scans each update before it
merges.

| Invariant | Enforced by |
| --- | --- |
| Every workflow runs on each pull request and on each push, except pushes to the Renovate, engine-bump and scanctl branches, whose pull requests test every push | `workflow:ci.yml`, `workflow:security.yml` |
| No secret is in the tree or its history | `scanctl:gitleaks`, `workflow:security.yml#scanctl`, `threat:CP1` |
| No env file is tracked, and `.gitignore` carries the env rule | `workflow:ci.yml#prose` |
| No licensed source and no Go in this tree | `audit:check-port`, `audit:check-grid`, `threat:CP3` |
| Dependencies and pinned images are vulnerability-gated | `scanctl:osv-scanner`, `scanctl:trivy`, `workflow:security.yml#scanctl`, `threat:CP5` |
| A CVE with a fix in a pinned image fails the weekly scheduled scan until a bump clears it or a dated acceptance covers it | `scanctl:trivy`, `workflow:security.yml#scanctl` |
| Static analysis runs on every change | `scanctl:semgrep`, `scanctl:zizmor` |
| GitHub Actions are pinned by digest, and a moved tag is not adopted by itself | `scanctl:zizmor`, `threat:CP6` |
| The installer builds, lints and tests green | `workflow:ci.yml#installer`, `workflow:ci.yml#entry-points`, `workflow:ci.yml#syntax`, `workflow:ci.yml#duplication`, `workflow:ci.yml#sentences` |
| A release tag on main publishes the installer, tested, as `catena-installer` through trusted publishing, versioned from the tag | `workflow:publish-installer.yml#publish` |
| The sign-on extension passes its tests and builds reproducibly: two builds give the same bytes | `workflow:ci.yml#keycloak-provider` |
| Code comments describe the tree as it stands and hold no history | `workflow:ci.yml#prose` |
| Core code names no application beyond a recorded debt that only shrinks | `workflow:ci.yml#prose` |
| Public prose names no system Catena does not ship | `audit:check-banned-words` |
| Every file and scenario is classified against the feature manifest, and none is left unclassified | `audit:check-grid`, `audit:check-all` |
| A gate pointer in this file that does not resolve, or a rehearsal scenario this file does not name, fails the build | `audit:check-public-specs`, `threat:CP7` |

## Gate pointers

`bench:<scenario>` is a rehearsal scenario on disposable virtual machines
driving the real product, and `bench:<scenario>#<stage>` the stage of it that
makes the claim; `audit:<gate>` is a static gate of the maintainers' audit
graph, run in CI and before every rehearsal; `workflow:<file>#<job>` is a CI job
in this repository; `scanctl:<tool>` is a scanner of the bundled security
workflow; `threat:<ID>` is an invariant in the maintainers' threat-model
register. A pointer that does not resolve or names a retired invariant fails
the build, and so does a scenario this file does not name.

## Declared, not yet built

Anything this file declares that the tree does not implement is listed here.

None.

## Installation

A server needs at least 4 GB of memory: 6 GB for light applications with few
users, 8 GB for heavier ones with many users. Catena's own services take about
2 GB. See [README.md](README.md).

| Invariant | Enforced by |
| --- | --- |
| The suite installs and runs on a server with less than 8 GB of memory, and the shared antivirus scanner reloads its signatures without holding two copies in memory | `bench:ce_install_suite`, `bench:clamd_reload_on_a_small_host`, `workflow:ci.yml#installer` |
| The heavier applications are tested on a server with 8 GB of memory | `bench:erpnext_update_migrates`, `bench:immich_extra_disk_round_trip` |

## Scenarios outside the invariants

Every rehearsal scenario is named in this file. The scenarios below prove no
invariant: a `skipped` scenario never runs its checks, and an `na` scenario
checks the bench, a mock or a third-party tool instead of the product.

| Scenario | Status | Why |
| --- | --- | --- |
| `bench:cloudflare_api_rotation_round_trip` | skipped | Needs a fresh Cloudflare token created for the run |
| `bench:tailscale_oauth_rotation_round_trip` | skipped | Needs a new private-network OAuth client created for the run |
| `bench:smtp_rotation_round_trip` | skipped | Needs a fresh mail-provider key created for the run |
| `bench:rotate_all_secrets_sequencer` | skipped | Waits for every single rotation scenario to run |
| `bench:dev_to_prod_cutover_round_trip` | skipped | Waits for per-application address reconciliation |
| `bench:debian_major_upgrade_restore` | skipped | Needs virtual machine images on two Debian major releases |
| `bench:pg_major_version_cross_restore` | skipped | Needs virtual machine images on two Postgres major versions |
| `bench:healthchecks_self_host_loss` | skipped | Waits for stages that wipe the job monitor and watch the outside heartbeat |
| `bench:fi_t3_start_sweep_failure` | skipped | Covered by the bench's unit tests |
| `bench:measure_converge` | na | Measures converge time |
| `bench:mixed_template_negative_restore` | na | Checks the bench's fixture engine |
| `bench:fi_c8_nextcloud_init_loop` | na | Checks the bench's diagnostics |
| `bench:fi_o4_hosts_yml_malformed` | na | Checks the YAML parser |
| `bench:fi_s2_input_truncated` | na | Checks the bench's configuration loader |
| `bench:fi_n2_cf_tunnel_down` | na | Checks the bench's Cloudflare preflight |
| `bench:fi_a3_keycloak_unreachable` | na | Checks a probe against a mock |
| `bench:fi_b6_healthchecks_down` | na | Checks a probe against a mock |
| `bench:fi_n3_dns_propagation_lag` | na | Checks a probe against a mock |
| `bench:fi_u3_gatus_baseline_down` | na | Checks a probe against a mock |
| `bench:fi_u4_ovh_rate_limited` | na | Checks a probe against a mock |
| `bench:fi_v2_external_scan_blocked` | na | Checks a probe against a mock |
| `bench:fi_s7_smtp_creds_invalid` | na | Checks the mail relay's refusal of a bad login |
| `bench:fi_b4_locked_pack_rotation` | na | Checks object lock on the bench's storage server |
| `bench:worm_object_lock_expiry_edge` | na | Checks object lock expiry on the bench's storage server |
| `bench:fi_d7_restic_corrupt_pack` | na | Checks restic's own data check |
