# SPEC.md

Catena-CE is an Ansible-based installer for Catena. It prepares a new Debian 13 server to host dockerized applications by providing a secure, ready-to-deploy platform. The main dashboard, `catena-admin`, is built from a closed-source repository and its image is available [on Github](https://github.com/catenahq/catena-admin/pkgs/container/catena-admin)

This file is the contract, and it leads the code: the tree implements this
file, not the other way round. Items declared here that the tree does not
implement yet are listed in [Declared, not yet built](#declared-not-yet-built).

AI agents: do not modify this file. Report any feature or code snippet in the
tree that this file does not cover, and any item in this file the tree does
not implement.

## Not in this repository

- **The dashboard source.** `catena-admin` is closed. This repository consumes
  its public signed image: it deploys the dashboard container and extracts the
  host engine binaries from that same image. No Go source belongs here.
- **The application catalogue.** Per-app compose files and templates live in
  `catena-templates`.
- **Maintainer tooling.** The rehearsal bench, the audit graph and the
  attestation are not published.
- **Secrets.** The repository contains none. What a deployment needs is
  documented in [ansible/SECRETS.md](ansible/SECRETS.md).

## Licensing

Catena-CE is complete and functional on its own. Catena Pro and Catena
Business are licences that unlock features of the dashboard and payload every
host already runs; the website's
[edition comparison](https://catena.run/en/#pricing) lists which.

A paid edition is a subscription key bought through Polar and saved in the
dashboard. The host asks Polar about the key every hour, and only when it
holds one. A key is active on one server at a time, bound to that server's
hardware, so a copy of the server is refused while the original holds the
key.

If an activation reaches Polar and its answer is lost, the server records it,
and the dashboard says to free that activation, labelled with the server's
domain, in Polar's customer portal, then to save the key again.

While Polar cannot be reached, the edition holds for 48 hours after its
last confirmation; a cancelled, revoked or freed key locks at once. The host
tells its admin of a lock once, in the maintenance log and by email through
the configured relay. A lock takes away the paid features only: applications,
their data, backups, restores and updates by hand keep running.

A move to another server needs a paid edition on the server being moved, and
none on the receiving one. The receiving server reaches the old one over SSH,
with a one-time migration ticket and a pairing code the old server's dashboard
gives out when it allows the move; no private network is needed, and no port
opens for it. Once the move is validated, the old server frees the key and
closes its migration window, and the receiving server takes the key over and
converges itself.

A paid server can send Catena support a report when one of Catena's own checks
fails or recovers, once its administrator switches it on in the dashboard. The
report names the check, its state and the application concerned, and nothing
from inside the applications; the text a failed job wrote stays on the server,
where the dashboard offers it for download, and reaches support only when the
administrator reviews it, edits it if they wish, and sends it. The dashboard
shows each report sent, and switching reports off stops them at once.

| Invariant | Enforced by |
| --- | --- |
| No key, an unconfirmed key or a refused one leaves the host on Community, and nothing fails closed | `bench:ee_ce_regression`, `bench:ee_lapse`, `threat:LE3` |
| A paid edition holds 48 hours while Polar is unreachable, locks at once on a refusal, and tells the admin once | `bench:polar_unreachable_grace`, `bench:ee_lapse`, `threat:LE3` |
| A lock takes away paid features only: applications, restores and Community actions keep running | `bench:license_seat`, `threat:LE3` |
| A subscription key is active on one server at a time | `bench:license_seat`, `bench:polar_sandbox_smoke`, `threat:LE7` |
| A move hands the key on only once the move is validated | `bench:wizard_migrate_round_trip`, `bench:migrate_lane_auth_denied`, `threat:LE12` |
| A move reaches the old server only through a one-time grant, while its migration window is open | `bench:migrate_lane_auth_denied`, `threat:CV15` |
| Error reports leave the server only while switched on and carry only the listed fields | `bench:error_reports_opt_in`, `threat:CV18` |

## How this repository is layered

A desired state is reached in four layers, widest to narrowest.

| Layer | What it is |
| --- | --- |
| **Installer** | `uvx catena-installer`, or `catena-installer <verb> --inventory <inventory>` in a console. Reaches the server over SSH; the server runs `ansible/install-host.sh` from the release image's own tree, which selects a playbook or chains several |
| **Playbook** | Names the hosts and the ordered list of roles to apply to them. One playbook is one atomic operation |
| **Role** | A unit of related tasks plus its defaults, templates, handlers and its own `validate.yml`. The reuse boundary: several playbooks apply the same role |
| **Task** | One step. Task files are shared between roles and between playbooks where the step is the same step |

Reuse happens downward only. A role never calls a playbook, and a task never
decides which role it belongs to.

## The installer

`catena-installer` (`installer/`) runs on the client's machine, Windows, macOS
or Linux, with uv: `uvx catena-installer`, or `uv run catena-installer` in a
checkout. With no argument it serves a page on loopback; with a verb it runs in
the console. It runs no Ansible itself: it reaches the server over SSH, and the
server installs itself with the code of the release image it fetched, so the
roles, the engines and the dashboard are one version.

The installer only reaches and installs a server: SSH access (address, initial
user, key, operator account), the admin email, and the settings the first
converge needs. A value the dashboard can change later is set in the
dashboard, never at install. Run again on an installed server, `install` is
the break-glass for a server whose dashboard is down. A verb that runs one
playbook is named after it.

| Verb | Runs | For |
| --- | --- | --- |
| `init` | nothing | Creating an inventory: its `.env`, every key at its default, to fill in before `install` |
| `install` | the `accounts` leg, then the `system` leg: `bootstrap`, `show-keyset`, `converge`, the version recorded, `validate`, and the keyset again; then the installer's outside checks | A fresh server, and a running one: run again, it asks nothing and re-applies the release the server records, the bootstrap roles included. `--release` moves the server to that release with that release's code. Reaches the server at its SSH address when one is set, else its public IP; `--address` reaches it at another address for one run, such as its tailnet address after a Lockdown, and `--tags` scopes the converge leg. Leaves public SSH open |
| `connect` | nothing | Opening the forward of the `panel` account, to the dashboard and Portainer, from the client's machine |
| `uninstall` | `uninstall.yml`, on the server | Handing the server back to Debian |

The `accounts` leg runs through the provider's login and creates the `ops` and
`panel` accounts with the installer's key; it closes nothing. The installer
then logs in again as `ops` with that key, and the `system` leg refuses to
start until sshd's own record of that session proves it, because its first
role turns off every other login. A leg whose connection drops runs to its
end on the server, which keeps a log of it without the passwords; an install
run again waits for it to end. After the `system` leg the installer scans
the public IP's every TCP port against what the server declares, and checks
that the container network is out of its reach.

A server that presents another SSH host key than the one the installer's
machine trusts for its address stops `install` before anything reaches it. A
reinstalled server presents a new key, and so does another machine answering at
that address: the person installing says which, with `--reinstalled` or the
installer page's reinstalled-server box. The old key is then forgotten, and
the installer trusts the new one.

`ansible/seed.py` runs on the client's machine before the install. It writes
the inventory and passes an optional admin password to the server as a
transient 0600 file for the run. It mints nothing, and refuses any value the
dashboard owns.

| Invariant | Enforced by |
| --- | --- |
| Installing and uninstalling go through the installer, and the server runs the tree of the release it installs | `bench:ce_install_suite`, `bench:install_rerun`, `bench:ce_uninstall` |
| A host runs the roles, the engines and the dashboard of one release | `bench:update_from_previous_release`, `workflow:ci.yml#installer` |
| Hardening waits until the server sees the installer's key open an `ops` session | `workflow:ci.yml#installer` |
| A verb that runs one playbook is named after it | `audit:check-grid` |
| The installer takes only what reaches and installs a server, and refuses a value the dashboard owns | `workflow:ci.yml#installer`, `bench:ce_install_suite` |
| An install whose connection drops runs to its end on the server, and an install run again waits for it | `bench:fi_o2_operator_ctrl_c#stage-1b-interrupted-install-ends-on-the-server`, `workflow:ci.yml#installer` |

## Playbooks

| Playbook | What it is |
| --- | --- |
| `accounts.yml` | The install's first leg, run on the server through the provider's login: the `ops` and `panel` accounts with the installer's key, and the sudo rule of `ops`. Closes nothing |
| `bootstrap.yml` | The half that cannot self-repair. Run by `install`, on the server, as the first step of its `system` leg |
| `converge.yml` | The full converge. Safe to re-run |
| `lockdown.yml` | The access posture, run on the host by the dashboard only. Joins the private network when one is saved. Closes public SSH behind it once the network proves it reaches the host, and opens public SSH when no private network is chosen |
| `reconcile.yml` | The half a host runs against itself, from the dashboard image, with no controller inventory. Has no verb: only the host runs it |
| `validate.yml` | Two vantages, both on the host: per-role checks, and access (the dashboard and Portainer on the host's loopback, the public hostnames once a domain is set, the published ports). The install's last step, after which the installer makes the checks only another machine can make; runnable on its own |
| `show-keyset.yml` | Right after bootstrap: mints the admin and console passwords if absent and shows them once, with the journal verification key. The install repeats the block when it ends |
| `rotate-tunnel.yml` | Deletes the host's Cloudflare tunnel and mints a new one, run on the host by the dashboard's Settings > Domain > Apply, followed by `reconcile.yml`. Applying the domain already in use is how the tunnel is regenerated. Takes no secret: it authenticates as the Cloudflare token already in the store |
| `rotate-tailscale.yml` | Forces re-authentication to the private network, run on the host by the dashboard |
| `uninstall.yml` | Hands the OS update lane back to Debian, releases the container engine's version hold, and prints teardown guidance |

`converge.yml` and `reconcile.yml` apply the same roles in the same order.
`reconcile.yml` is `converge.yml` minus the roles a human must run and minus
the steps that only make sense on a fresh box. Which roles that leaves is
declared, not decided at the moment of extraction.

| Invariant | Enforced by |
| --- | --- |
| Re-running a converge repairs a drifted server; it never duplicates or breaks a healthy one | `bench:install_rerun`, `bench:converge_suite#stage-7c-rotated-secret-arrives`, `bench:converge_suite#stage-7b-bump-survives` |
| A host converges itself from the image with no controller inventory | `bench:payload_action_dispatches_without_converge` |
| Validation checks both directions: services answer, forbidden exposure does not | `bench:ce_validate` |
| Uninstall hands the OS update lane back to Debian | `bench:ce_uninstall` |

## Roles

### The boundary

Two questions, asked in order, decide which side a role is on.

1. If this is wrong, is anything left that can fix it? If no, it is
   **Bootstrap**: applied by `install`, on the server, from the release image.
   The set is small and is meant to stay small.
2. Otherwise, is the correct value a function of the product VERSION or of
   THIS HOST? Either way the role is **Reconcile**: it can run on the host,
   unattended, from the image, on a timer.

A role's side is where it lives: `ansible/bootstrap/roles/` or
`ansible/reconcile/roles/`. `ansible/boundary.yml` is the declaration, because
a directory cannot carry the reason a role is on the side it is on.
`ansible/tests/unit/test_converge_boundary.py` holds the two to each other.
Nothing straddles the line.

### Bootstrap roles

Each entry carries the reason it is here, because a set that grows by
assertion grows.

| Role | Why it cannot self-repair |
| --- | --- |
| `common` | The accounts, SSH, the ufw lockdown, and the packages the other bootstrap roles assume. Opens public SSH, and holds the tasks the dashboard's Lockdown runs to close or open it |
| `host_hardening` | Kernel and module hardening, applied before dockerd's first start so its runtime writes do not win until the next reboot. Re-applying it under a live workload is not a thing to do unattended |
| `tailscale` | The private network the host is reached over, when one is configured. Joined only by `lockdown.yml`. Getting it wrong is the definition of question one |
| `storage` | The data prefix every service keeps its data under, on the disk the host already has, and the optional remote bulk mount. A failure here costs data rather than access, and it cannot be safely re-applied to a live host by a timer |
| `docker` | The engine, at the version pinned here and held on the host, and the swarm. Nothing else runs without it, and that includes whatever would have repaired it |
| `catena_admin_host` | Who the dashboard's dispatch signs in as and with what: the runner account, the dashboard's key pair and the host key it trusts; and the way a server taking this one over arrives, the migration account and its sshd rules, which nothing on the host can prove without an open migration window. A reconcile that could mint, move or trust another key could rewrite what a reconcile is |
| `ansible_runtime` | The pinned ansible-core the host reconciles itself with. Also performed by the reconcile lane, deliberately: a root process with python3 can repair a broken runtime, so a host whose runtime broke does not need a human |

### Reconcile roles

Order is the converge order, which is dependency order.

| Role | Contract |
| --- | --- |
| `dispatch_path` | The path a dashboard request crosses to become a root action: sshd's variable list for the runner account, its sudo rule, the forced command on the dashboard's key and the dispatcher, kept on the generation this tree ships. A new generation is installed beside the running one, and the dashboard's key moves to it only after a request has crossed it; a failed proof keeps the running path and stops the converge. First, so a proof that fails stops the converge before anything else changes |
| `payload` | Installs the host engine binaries, the lane scripts and their units, restic and rclone, by extracting them from the dashboard image the converge names, refused unless that image is, by digest, the release the converge's own tree came from and was built from this tree. Deploys no container |
| `public_ports` | The declarative public-port registry's host side: the fragment directory, the docker drop-in that reapplies the rules on every docker start, and the reconciler's timer. The reconciler and its timer ship in the payload, and a payload without them fails the converge |
| `host_maintenance` | The OS update configuration: unattended-upgrades and its origins (Debian security, stable updates and point releases, and the private-network client's own repository), the restart policy for services left on replaced libraries, and apt's settings. Also the baseline packages, the journal's storage, the host resolving its own name, the clock's NTP pool, the host mail transfer agent kept off port 25, the time zone and locale set in the dashboard, the reboot-required probe's configuration, and the timers of the licence check and the error reports. The probe, the licence check, the error reporter and their timers ship in the payload |
| `swarm` | The swarm's own settings (task history, the data-node label), which a restore does not bring back, and the self-heal that restarts containers which lost the race to the overlay at daemon start |
| `traefik` | The reverse proxy and the `catena-network` overlay |
| `postgres` | The infrastructure Postgres swarm service, hosting the sign-on database |
| `portainer` | The container control plane |
| `cloudflare_tunnel` | The cloudflared swarm service, the tunnel and wildcard DNS. Self-defers when the store holds no Cloudflare token |
| `keycloak` | The sign-on identity provider and its realm |
| `oauth2_proxy` | One auth proxy per gated admin tool, each keeping its own sign-in session |
| `infrastructure` | Gatus, Healthchecks, Beszel and its agent, the antivirus watch, the mail canary, the external heartbeat's configuration, the application wiring scripts, and the configuration of the dashboard and Gatus sync lanes, whose scripts and timers ship in the payload |
| `catena-admin` | The action catalogue, the bind-mount targets, the update lanes' per-host configuration and the list of services they may update, the dashboard's port declarations, and the dashboard container, created directly against the local swarm. It holds the key that drives Portainer, so it cannot depend on Portainer to run. The image is the release the install fetched, or, on a converge the host runs itself, the image the dashboard runs; a converge that names none stops before any role runs. An install fetches the release it is given, else the one the server records, else the newest the registry names |
| `coturn` | The shared TURN and STUN relay for the audio and video media plane |
| `backup` | The backup lane's per-host configuration (repository, credentials, paths, excludes) and the first snapshot. restic, rclone, the backup wrapper and its units ship in the payload |

### How a role is reached

Three ways, and the difference is load-bearing. All three are declared in
`boundary.yml`, each entry carrying how it is reached, so a role outside the
converge's `roles:` list is classified rather than exempted.

| Reached as | Roles | Meaning |
| --- | --- | --- |
| A converge role | The two tables above | Applied by `converge.yml`, and by `reconcile.yml` for the reconcile half |
| A post-task role | `tier1_stack` | A fold over the converge rather than a step in it: the control-plane roles each append their service spec to an accumulator, and this renders the accumulated set and type-checks it against the docker installed. It applies nothing, and runs from `post_tasks` once every contributor has |
| An own-playbook role | `cloudflare_tunnel_regenerate` | Deletes this host's tunnel before handing back to `cloudflare_tunnel` to mint a new one. A converge able to do that would drop the public edge every run, so the delete is only ever an explicit request: the dashboard's Settings > Domain > Apply, which runs `rotate-tunnel.yml` before its converge |

### Paths a reconcile task may never write

`/etc/ssh/`, `/etc/sudoers.d/`, `authorized_keys`,
`/usr/local/sbin/catena-admin-runner`, `/usr/local/lib/catena-dispatch/`,
`/etc/sysctl.d/`, `/etc/modprobe.d/`, `/etc/ansible/facts.d/`.

These are bootstrap-owned. A reconcile that can rewrite the way in can rewrite
what a reconcile is, so the rule is a list of literals that can be checked
rather than remembered.

The one exception is the `dispatch_path` role, declared under `proven_switches`
in `boundary.yml`: it writes only the paths of the dispatch path, and changes
the live path only after a request has crossed the new one. The accounts, the
dashboard's key pair, the host key and `known_hosts` stay bootstrap-owned.

Two qualifiers, both deliberate. The rule covers **task files only**: a
defaults file may name `/etc/ssh` because restic reads it into the backup set,
and reading a path is not writing it. And comments are stripped before
matching, so the file that explains why a reconcile must not write the forced
command is allowed to say so.

### Paths a bootstrap task may never write

The unattended-upgrades and needrestart configuration, the journal's drop-ins,
the clock's NTP drop-in, the update lanes' configuration, the dashboard's port declaration and the
overlay self-heal. The reconcile side keeps them current on every converge,
from either path. A bootstrap task runs only at install, so a second writer
there would hold the host to the install's value until the next converge.

### Reachability, not location

A reconcile-side task file that only its own operator playbook reaches, never
a converge, may be declared under `operator_only_files` in `boundary.yml`, and
the rule then does not apply to it. The exemption is about what can reach the
file, not where the file lives: a converge may not reach it, and a timer may
not reach it. None is declared.

| Invariant | Enforced by |
| --- | --- |
| Every role is on exactly one side, with a stated reason if it is bootstrap | `audit:check-grid` |
| No reconcile task writes a bootstrap-owned path, and no reconcile play reaches a bootstrap task file | `bench:ce_install_suite` |
| A restore is neither a CLI verb nor a converge step: it runs only when started on the host | `workflow:ci.yml#installer`, `bench:ce_restore`, `bench:backup_rollback` |
| The reconcile half reads nothing from a controller inventory | `bench:payload_action_dispatches_without_converge` |
| The install's converge and the host's own converge produce the same host: after either, the other changes nothing | `bench:reconcile_is_a_noop` |
| A host that converges itself puts back the upkeep it owns | `bench:reconcile_repairs_host_upkeep` |
| A dashboard update changes the way a dispatch arrives only after a request has crossed the new path; a failed proof keeps the running path | `bench:update_from_previous_release` |
| A converge refuses engines from an image built from another tree, and root runs nothing out of a place the dashboard can write | `bench:update_from_previous_release`, `workflow:ci.yml#installer`, `threat:CV14` |

## State the tasks produce

Six things outlive the run that wrote them. Each belongs to the role or task
that produces it.

### The on-box store -- `common`, read by every role

`/etc/catena/config.json`, 0600 root, is the runtime source of truth: the
admin password an install supplies, every setting and credential saved in the
dashboard, and every internal secret, minted on the host. It rides the backup.
The inventory is bootstrap input only, with one exception: the
development-only certificate keys, which no dashboard field shows, are written
from the inventory into the store on every install run.
`ansible/helpers/onbox_config.py`
declares which names are secrets. Every converge loads the store first, so a
tag-filtered run reads the same values as a full one.

| Invariant | Enforced by |
| --- | --- |
| The store is 0600 root, holds the full secret set, and rides the backup | `bench:ce_install_suite`, `bench:recover_secrets_from_running_host`, `threat:CV3` |
| A tag-filtered converge reads the store, not a stale inventory value | `bench:converge_suite#stage-7c-rotated-secret-arrives` |

### The public-port registry -- `public_ports`

A port is declared before it opens: infrastructure roles drop a JSON fragment
into `/etc/catena/public-ports.d/`, application templates carry
`vps.expose.*` compose labels. The reconciler the payload ships,
`catena-public-ports`, merges both into ufw and DOCKER-USER rules with the
label vocabulary of the dashboard image the host runs, and validation and the
external scan check against the merged set.

ufw is default-deny. No web port is bound on the host: web traffic enters
through the Cloudflare tunnel. The ports bound on the public IP are the TURN
relay's UDP media plane, present only while a consumer runs, and the non-web
ports an application template declares, such as mail, a video bridge's UDP
media and a SIP gateway.

SSH is key-only, with no root and no password login. Public port 22 follows
the private network:

| Private network | Public 22 |
| --- | --- |
| None | Open. Nothing closes it |
| Joined | Open until the Lockdown is applied |
| Joined, Lockdown applied | Closed once the network proves it reaches the host; reopened by the port reconciler while the network is down |
| Set back to none after a Lockdown | Closed, and reopened while the old network is down, until the access is applied again, which opens it |

No domain, tunnel or private network is configured at install. Each is saved
in the dashboard, which checks the credential on the host first and stores
nothing it refuses. Until a domain exists, the dashboard is reached through a
forward-only SSH account (`panel`) to the host's loopback.

| Invariant | Enforced by |
| --- | --- |
| No web port is open on the server itself; an external scan proves it | `bench:security_scan`, `bench:fi_v2_external_scan_blocked`, `threat:CV1` |
| Every open port is declared before it is opened | `bench:security_scan`, `bench:ce_validate` |
| A port declaration is read with the label vocabulary of the dashboard image the host runs, so an image update changes it with no converge | `bench:labels_vocabulary_from_image` |
| SSH is key-only with no root login; public 22 closes only behind a proven private network, reopens while that network is down, and opens again when no private network is applied | `bench:security_scan`, `bench:fi_n8_ufw_concurrent_ssh`, `bench:ce_install_suite`, `threat:CV6` |
| A server installs with no tunnel credential and brings its public edge up later without a reinstall | `bench:ce_install_suite` |
| A credential entered in the dashboard is checked on the host before it is stored, and a refused one is never stored | `bench:ce_install_suite`, `bench:fi_s4_tailscale_oauth_revoked` |
| The tunnel can be replaced on a live host from the dashboard, without a reinstall | `bench:cf_tunnel_regenerate_round_trip` |
| A domain token may reach several domains; only the ones chosen in the dashboard are attached, and Community attaches one | `bench:fi_n10_multidomain_cap` |
| The private-network control server is pluggable | `bench:ce_install_headscale` |

### The restic repository -- `backup`

Backups are encrypted on the host and go to an S3 repository set in the
dashboard. The restic password is generated on the host once the repository
and its keys are saved, and shown once: the host keeps it to run backups, and
the copy kept off the host opens the repository once the host is gone. A
server rebuilds from the endpoint and that keyset alone.

restic and rclone ship in the payload, pinned by digest. Saving a backup
destination in the dashboard creates its repository when the bucket holds
none. A backup never creates one: it stops when it finds no repository, or
when the password is wrong. Snapshots list, browse and export without a
restore.

Restores run on the host through `catena-recovery`, the payload's restore
engine, which the dashboard's restore page drives. A restore reads the
repository it is given the same way whichever it is (this server's own,
another server's, or an offsite copy) and writes nothing to it, so a key that
may only read is enough. A whole-server restore puts the data back, adopts
the settings stored with it, renders the server's services from them, starts
the services Catena runs, and joins the private network those settings name.
The client applications it brought back stay stopped until an administrator
starts them from the restore page, which replays their databases, releases
them from their backup mode and brings the server up to date with a converge:
a restore cannot tell whether their data was compromised before the backup was
taken. A one-application restore holds that application the same way; a move
to another server holds nothing. After a whole-server restore, backups wait
until an administrator confirms the backup destination or saves a new one (a
converge meanwhile reports them waiting, not failed), and
the daily chain's updates and engine upgrade are turned off, with a warning
on every page, until an administrator turns them back on: a restore never
treats the repository it read, or the one its settings name, as safe to write
to. A new destination gets a new backup password, shown once. Progress is
recorded in
`/var/lib/catena/recovery.state`: an interrupted restore resumes with the same
snapshot and scope, refuses a different one, and only a finished run clears
the record.

On a Catena Business server the offsite copy duplicates a bucket into a
locked bucket at another provider, and refuses a destination that holds a
different repository from its source. A bucket comes back from its offsite
copy into a bucket and keys the administrator names, new ones after a
compromise, never into the bucket it came from, and the application that used
it is switched to that bucket, its settings put back if the switch fails. The
switch runs the application's commands once its containers that read the
bucket run on the new one. The restore page lists the repositories a locked
bucket holds. The backup
repository comes back the same way: backups then either continue in it, with
its offsite copy continuing in the same locked bucket once that copy proves to
hold the same repository, or start in a new repository whose copy goes to a
new folder of that bucket.

| Invariant | Enforced by |
| --- | --- |
| A server rebuilds from only the backup endpoint and key | `bench:dr_suite#stage-19-onbox-store`, `bench:ce_restore`, `bench:recover_secrets_from_running_host`, `threat:CV9` |
| Backups are encrypted on the host before upload, under a password also kept off the host | `bench:dr_suite#stage-13-disaster-recovery`, `bench:ce_restore`, `threat:CV4` |
| Backups restore -- rehearsed, not assumed | `bench:backup_rollback`, `bench:ce_restore`, `bench:erpnext_restore_round_trip` |
| A bucket put back into a new bucket switches the application that used it | `bench:worm_round_trip` |
| A restore writes nothing to the repository it reads | `bench:worm_round_trip` |
| After a restore, the client applications stay stopped until an administrator starts them | `bench:inplace_restore_suite`, `bench:dr_suite` |
| After a whole-server restore, backups wait for a confirmed or new destination | `bench:rebuild_backs_up_after_restore`, `bench:dr_suite` |
| An interrupted restore resumes with the same snapshot and scope, and is never reported as finished | `bench:fi_n5_provider_outage_mid_restore`, `bench:inplace_restore_suite#app-restore-stage-6-halted-scope-refuses-to-widen` |
| Snapshots export without a restore | `bench:snapshot_export_round_trip` |
| The backup key rotates without losing the repository | `bench:restic_password_rotation_round_trip` |
| Deleting the dashboard costs convenience, never data: backups run, restores work, applications stay online | `bench:sovereign_exit`, `bench:recovery_readme_manual_restore` |

### The timers -- `public_ports`, `infrastructure`, `host_maintenance`

Scheduled work is default-deny. This repository ships two local-maintenance
timers (the antivirus watch, the mail canary) and enables six the payload
ships (the public-port reconcile, the dashboard and Gatus syncs, the hourly
reboot-required probe, the hourly licence check, and the five-minute error
reports to Catena support, which send nothing until an administrator switches
them on). None spends object storage.

Every lane (backup, the bit-rot check, the restore test, the offsite copy, the
daily update chain, dashboard updates, the scheduled converge, the off-site
heartbeat, the vulnerability watch) ships in the payload with its timer off.
`catena-schedule apply` turns a lane on only on a licensed host, at
the cadence set in the dashboard. The backup, the updates, the converge, the
container engine upgrade and the restart stay runnable by hand on every host.
A dashboard update applies the configuration its new image carries with a
converge, and puts the previous dashboard back when that converge fails.

The local Healthchecks checks of the lanes follow the lanes' schedules, and a
lane that is off or unlicensed has its checks paused. The external heartbeat is
the one call that leaves the server: while its lane is on and the licence
active, it calls the outside monitoring address saved in the dashboard, and
names any of the server's own monitors that stopped answering.

Around the nightly backup, each application whose compose file declares a
backup mode is put into it and taken back out, also after a failed backup and
before a resumed run backs up again; a restore takes restored applications out
of it. The commands run only inside that application's own container. A
command still running when its time is up gets as long again, two minutes at
most, to end, and is then stopped before the next command runs in that
application. An application left in its backup mode, and a command that had
to be stopped, are named in the maintenance log. An update holds the
application's own commands to their time the same way: a command stopped
before the update refuses it, and a stopped migration rolls the update back.

The daily chain applies updates whether or not a backup is configured, and the
dashboard warns on every page for as long as none is. While the backups wait
after a restore, the chain still takes its updates when their steps are on, and
the maintenance log says no backup stands behind them. The dashboard's
Schedules page chooses the chain's steps; a server that never chose runs them
all except the vulnerability watch and the restore test. The restore test
restores the latest backup into a scratch area and checks it, on its own weekly
lane or as a step of the chain, never both. Once a week it also starts the
restored databases, Postgres and MariaDB, and Nextcloud on a network of their
own with no published port, and checks that each opens. An offsite copy of the
host's backup repository always runs behind a repository check and is refused when it fails;
an option makes it wait for a restore test of the newest backup too. Leaving
out the backup or its repository check, scheduling no restore test, leaving out
the health check before the updates, or leaving out the offsite check is
allowed with a warning, and updates running without the backup step put a warning at the top
of every page. A configured backup that fails, or fails its verification,
stops the chain before any update, unless the backup step is left out. After
the container updates, the chain upgrades the container engine, when that step
is on, to the version pinned in this repository, within the major version it
runs, and restores the previous version when the host does not come back
healthy. Its last step, when on, restarts the host when an update needs one,
including the operating system updates, also after a night that stopped before
any change, and checks afterwards that every service came back. The chain
reports to its own alert check and records its result, or the step that
failed, in the maintenance log.

Jobs that change the server or its backup repository run one at a time:
backups and their checks, the offsite copy, updates, converges, access changes,
restores, the daily chain from its first step to its last, the scheduled
dashboard and firewall syncs, and the backup password and repository actions.
A scheduled job waits for the one running; a night that cannot start within an
hour is skipped, reported, and runs again the next night; a change made in the
dashboard meanwhile answers that the server is busy. Firewall rules are
reapplied on every container engine start without waiting.

The vulnerability watch runs on its own schedule or as a step of the daily
chain, never both. It checks the software lists of every running image against
a fresh vulnerability database and the list of vulnerabilities known to be
exploited, and alerts once per vulnerability. An emergency patch moves an
application's service to the oldest release its update policy allows that a
scan on the host shows free of the vulnerability, behind the update's own
copies, health check and rollback; a person confirms it, or, when the
administrator turns the automatic mode on, the watch starts it for an exploited
vulnerability, never for a service kept off the updates and never while
updates are paused.

Debian's unattended-upgrades is the one thing that applies OS packages, on
every host, licensed or not: the security suite, stable updates and point
releases, and the private-network client from its own repository. A service
left running on a replaced library restarts after the update; the container
engine waits for a restart of the host. The dashboard shows a pending restart
on every page until it is taken.

| Invariant | Enforced by |
| --- | --- |
| Scheduled work is default-deny: this repository ships only enumerated local-maintenance timers, and every lane ships in the payload | `audit:check-port`, `threat:CV8` |
| An unlicensed host schedules no lane at all | `bench:unlicensed_schedules_nothing` |
| A host that needs a restart reports it, and without a licence waits for a person | `bench:reboot_required_notified` |
| A failed backup verification stops the daily chain before any update, while the restore test is in the chain | `bench:daily_chain_verify_hot_fail_aborts_updates` |
| The weekly restore test starts the restored databases and Nextcloud and checks that each opens | `bench:verify_hot_bootprobe_weekly`, `bench:erpnext_restore_round_trip` |
| Jobs that change the server or its backup repository run one at a time, and a dashboard change made meanwhile answers busy | `bench:concurrent_backup_lock_contention`, `bench:daily_chain_step_list` |
| An emergency patch moves a service only to a release within its policy that a host scan shows free of the vulnerability | `threat:CV16` |
| The applications are in their backup mode for the whole nightly backup and out of it after a failed one | `bench:daily_chain_quiesce_invoked` |
| A backup-mode command that outlives its time has ended before the next one runs | `bench:app_declarations_travel_with_the_app#stage-6b-a-backup-mode-past-its-time-ends-first` |
| The external heartbeat calls out while the monitors answer, names a silent one, and stops when turned off | `bench:external_heartbeat_round_trip` |

### The release manifest -- written last, by both converge paths

Every converge, from `converge.yml` or `reconcile.yml`, records what it
delivered. Before that, it prunes files an earlier converge installed and this
one no longer ships, provided the file is unchanged and the payload manifest
(`/var/lib/catena/payload-manifest.json`) does not claim it.

| Invariant | Enforced by |
| --- | --- |
| Every converge records what it delivered | `bench:converge_suite#stage-7a-the-action-is-back` |
| Withdrawn files are removed, and Community files are not pruned as though they were licensed | `bench:payload_prune_respects_ce` |
| A converge never prunes a file the payload claims | `workflow:ci.yml#installer` |

### The version stamp -- written first, by both converge paths

`/etc/catena/version.txt` names the version of this repository a converge
applied, as `git describe`, from the provenance the release image records beside its
tree: the image the install fetched, or the one the dashboard runs on a
converge the host runs itself. A tree with no such record stamps nothing and
leaves the previous stamp standing. It is the first thing either path writes,
and it rides
the backup, so every snapshot names the version that produced its data.

A restore compares the snapshot's stamp with the host's: a newer snapshot is
refused, because a newer database directory does not open under an older
database; an older one needs an explicit upgrade; two stamps that cannot be
ordered are refused. An upgrade runs each restored application's declared
migration once its data is back, and never stops one part-way, since a half
migration cannot be undone: it waits for it, half an hour at least, and
reports one still running then. A move between two servers is refused unless
both run the same version.

## Applications

The dashboard deploys applications from a catalogue resolved per host: domain
names, sign-on, and a password per application generated on the host. Per-app
compose lives in `catena-templates`; this repository owns the wiring that makes
the suite one product: mail, chat and video calling, file and office
integration, the antivirus watch and delivery canaries. The action that wires
Nextcloud Talk to the call relay installs Talk from Nextcloud's app store when
it is missing, and changes nothing when the store cannot be reached.

The host's sync lane, which sets up each application's sign-in and keeps
mailboxes in line with the sign-on groups, reaches the identity provider
through the dashboard on the host's loopback rather than through the public
edge, so both keep working while the tunnel is down. The dashboard forwards
only the calls that lane makes, behind the catalogue's per-host token, and adds
no credential of its own.

Settings > Outgoing mail is the source of the server's mail settings. A
converge writes them into the sign-in realm while the realm still holds the
settings Catena wrote; settings changed in the realm's own Email tab stay.
Alerts for scheduled jobs and services reach the admin email by default,
through the outgoing mail; with outgoing mail off they reach only the channels
added in Healthchecks.

Each shared service answers on `<subdomain>.<domain>`: `auth` (the sign-on),
`dash` (the dashboard), `portainer`, `gatus`, `healthchecks` and `beszel`. Each
subdomain is a setting in the dashboard; a host with none stored uses that
name. Gatus names each service it monitors without its version, so a service
keeps its history across updates; the dashboard shows administrators the
versions.

| Invariant | Enforced by |
| --- | --- |
| The catalogue is resolved per host, and a malformed entry is refused | `bench:marketplace_catalog_resolved`, `bench:malformed_catalog_rejection` |
| A compose file that does not load is rejected before it reaches a host | `bench:fi_u1_compose_lint_reject` |
| A broken template is repairable in place | `bench:repair_broken_template_round_trip` |
| The overlay network heals itself | `bench:swarm_overlay_selfheal` |
| Monitoring reports a service that is down rather than assuming it is up | `bench:fi_u3_gatus_baseline_down` |
| Single sign-on secrets rotate without breaking the suite | `bench:keycloak_signing_keys_rotation_round_trip`, `bench:oauth2_proxy_cookie_rotation_round_trip` |
| Every admin surface sits behind the identity gate | `threat:CV2` |
| The dashboard dispatches its actions without a converge, and refuses an action it does not know | `bench:ce_admin_smoke`, `bench:ce_admin_actions`, `bench:admin_action_unknown_rejected` |
| The record of administrative actions can be exported and checked off the box: an edited or shortened trail fails verification | `bench:audit_chain_tamper_evident`, `threat:CV10` |
| Application sign-in provisioning and mailbox reconciliation keep running while the Cloudflare tunnel is down | `bench:mailbox_sync_ownership#stage-5-sync-without-the-tunnel` |
| An application that asks for single sign-on gets its own sign-in entry, kept in step with its labels and removed with the application; entries made by hand are left alone | `bench:oidc_label_client_provisioned`, `threat:CV13` |
| Catalogue applications that offer single sign-on in their free edition sign in with the suite's account | `bench:catalog_app_sign_in` |
| A sign-in cookie a client application or another admin tool sees opens no admin tool | `bench:app_container_cannot_claim_an_identity`, `threat:CV2` |
| No sign-in gate shows its credentials in its service definition | `bench:oauth2_proxy_cookie_rotation_round_trip`, `threat:CV7` |
| An application keeps the backup mode, update steps and restore steps it was deployed with; the server needs no catalogue to back it up, update it or restore it | `bench:app_declarations_travel_with_the_app` |
| Monitoring probes each shared service, Beszel's hub included | `bench:gatus_probes_beszel_hub` |
| Mail settings changed in the sign-in realm's own Email tab survive a converge and a change of the panel's outgoing mail | `bench:realm_hand_edits_survive_converge` |
| The Talk action installs Talk and wires it to the call relay and the signalling server, or changes nothing | `bench:suite_wiring_commands#stage-talk-hpb-wired`, `workflow:ci.yml#installer` |

## CI

| Workflow | Jobs | Gates |
| --- | --- | --- |
| `ci.yml` | `installer`, `entry-points`, `syntax`, `duplication`, `sentences`, `prose` | the Ansible tree's and the installer's unit tests, the one entry point a client runs (`catena-installer`) with its locks and its wheel, Ansible syntax, copy-paste against a ratchet threshold, duplicate sentences, and comment prose |
| `publish-installer.yml` | `publish` | on a release tag on main, the installer's tests, then its wheel published to PyPI as `catena-installer` through trusted publishing, versioned from the tag |
| `security.yml` | `scanctl` | secrets, dependency vulnerabilities, static analysis, and the container images pinned in role defaults. A finding in this tree fails every run; a CVE in a pinned image fails the change that introduces it, and the weekly scheduled run whenever a fix exists, until a bump clears it or the committed baseline accepts it until a date. A pin that stops matching fails the scan |

Every workflow runs on each pull request and on each push, except pushes to
the Renovate and engine-bump branches, whose pull requests test every push.

Images pinned in role defaults move through the catena-admin update engine,
run nightly from catena-admin's engine-bump workflow (seven-day soak, CVE
gate, upgrade stops), one pull request per defaults file. Every other
dependency follows [renovate](https://github.com/catenahq/renovate) and
[renovate-config](https://github.com/catenahq/renovate-config) under a
seven-day cooldown, with GitHub Actions pinned by digest. The container
engine's version pin follows the same path under a fourteen-day cooldown; a
new major version is merged by hand, after a rehearsal proves it.
[scanctl](https://github.com/catenahq/scanctl) scans each update before it
merges.

| Invariant | Enforced by |
| --- | --- |
| No secret is in the tree or its history | `scanctl:gitleaks`, `workflow:security.yml#scanctl`, `threat:CP1` |
| No licensed source and no Go in this tree | `audit:check-port`, `audit:check-grid`, `threat:CP3` |
| Dependencies and pinned images are vulnerability-gated | `scanctl:osv-scanner`, `scanctl:trivy`, `workflow:security.yml#scanctl` |
| A CVE with a fix in a pinned image fails the weekly scheduled scan until a bump clears it or a dated acceptance covers it | `scanctl:trivy`, `workflow:security.yml#scanctl` |
| Static analysis runs on every change | `scanctl:semgrep`, `scanctl:zizmor` |
| The installer builds, lints and tests green | `workflow:ci.yml#installer`, `workflow:ci.yml#entry-points`, `workflow:ci.yml#syntax`, `workflow:ci.yml#duplication`, `workflow:ci.yml#sentences` |
| Code comments describe the tree as it stands, not its history | `workflow:ci.yml#prose` |
| Public prose names no system Catena does not ship | `audit:check-banned-words` |
| Every file and scenario is classified against the feature manifest; nothing untracked | `audit:check-grid`, `audit:check-all` |
| This file cannot drift from reality | `audit:check-public-specs`, `threat:CP7` |

Gate pointers: `bench:<scenario>` is a rehearsal scenario on disposable
virtual machines driving the real product; `audit:<gate>` is a static gate of
the maintainers' audit graph, run in CI and before every rehearsal;
`workflow:<file>#<job>` is a CI job in this repository; `scanctl:<tool>` is a
scanner of the bundled security workflow; `threat:<ID>` is an invariant in the
maintainers' threat-model register. A pointer that does not resolve, or names
a retired invariant, fails the build.

## Declared, not yet built

This file leads the code. Anything it declares that the tree does not
implement is listed here, so the gap is a finding rather than a silence.

None.

## Installation

A server needs at least 4 GB of memory: 6 GB for light applications with few
users, 8 GB for heavier ones such as Nextcloud or ERPNext with many users.
Catena's own services take about 2 GB. See [README.md](README.md).

| Invariant | Enforced by |
| --- | --- |
| The suite installs and runs on a server with less than 8 GB of memory, and the shared antivirus scanner reloads its signatures without holding two copies in memory | `bench:ce_install_suite`, `bench:clamd_reload_on_a_small_host`, `workflow:ci.yml#installer` |
| The heavier applications are rehearsed on an 8 GB server | `bench:erpnext_update_migrates`, `bench:immich_extra_disk_round_trip` |
