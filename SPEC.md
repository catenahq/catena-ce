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
- **Maintainer tooling.** The rehearsal bench, the audit graph and the fleet
  control plane are not published.
- **Secrets.** The repository contains none. What a deployment needs is
  documented in [ansible/SECRETS.md](ansible/SECRETS.md).

## Community vs Catena Pro

Catena-CE is complete and functional on its own. Every host keeps its
operating system current through Debian's unattended-upgrades and notifies when a reboot is pending. Every other update is a button.
Catena Pro adds automation on top of the same host-native operations:
scheduled backups at any frequency, managed updates with rollback, including
the container engine and the restart a kernel update needs, daily maintenance,
offsite immutable secondary backups, attestation, central audit shipping, multiple
sign-on domains, and server-to-server migration.

## How this repository is layered

A desired state is reached in four layers, widest to narrowest.

| Layer | What it is |
| --- | --- |
| **CLI** | `uv run catena-cli <verb> --inventory <inventory>`. Selects a playbook, or chains several. Prompts for what it needs, and never asks Ansible anything the product cannot answer for itself |
| **Playbook** | Names the hosts and the ordered list of roles to apply to them. One playbook is one atomic operation |
| **Role** | A unit of related tasks plus its defaults, templates, handlers and its own `validate.yml`. The reuse boundary: several playbooks apply the same role |
| **Task** | One step. Task files are shared between roles and between playbooks where the step is the same step |

Reuse happens downward only. A role never calls a playbook, and a task never
decides which role it belongs to.

## CLI and GUI

Both run from the repository root: `uv run catena-cli <verb> --inventory
<inventory>` (`ansible/catena_cli.py`), and `uv run catena-gui`, a local web
front end that produces the same install input.

The installer only reaches and installs a server: SSH access (address, initial
user, key, operator account), the admin email, and the settings the first
converge needs. A value the dashboard can change later is set in the
dashboard, never at install. The other verbs are the break-glass for a server
whose dashboard is down. A verb that runs one playbook is named after it.

| Verb | Runs | For |
| --- | --- | --- |
| `install` | seed, then `bootstrap`, `show-keyset`, `converge`, `validate`, and the keyset again | A fresh server. Runs over the public SSH connection it started on, and leaves public SSH open |
| `converge` | `converge.yml` | A running server. `--address` reaches it at another address for one run, such as its tailnet address after a Lockdown |
| `uninstall` | `uninstall.yml` | Handing the server back to Debian |

`ansible/seed.py` runs before Ansible, on `install` only. It writes the
inventory and passes an optional admin password to the first converge. It
mints nothing, and refuses any value the dashboard owns.

| Invariant | Enforced by |
| --- | --- |
| Installing, converging and uninstalling go through the bundled CLI | `bench:ce_install_suite`, `bench:install_rerun`, `bench:ce_uninstall` |
| A verb that runs one playbook is named after it | `audit:check-grid` |
| The installer takes only what reaches and installs a server, and refuses a value the dashboard owns | `workflow:ci.yml#installer`, `bench:ce_install_suite` |

## Playbooks

| Playbook | What it is |
| --- | --- |
| `bootstrap.yml` | The half that cannot self-repair. Run by hand, over SSH, from outside |
| `converge.yml` | The full converge. Safe to re-run |
| `lockdown.yml` | The access posture, run on the host by the dashboard only. Joins the private network when one is saved. Closes public SSH behind it once the network proves it reaches the host, and opens public SSH when no private network is chosen |
| `reconcile.yml` | The half a host runs against itself, from the dashboard image, with no controller inventory. Has no verb: nothing on a controller dispatches it |
| `validate.yml` | Three vantages: on-host per-role checks, access from the controller, external from the controller. The install's last leg, and runnable on its own |
| `show-keyset.yml` | Right after bootstrap: mints the admin and console passwords if absent and shows them once, with the journal verification key. The installer repeats the block when the install ends |
| `rotate-tunnel.yml` | Replaces the host's Cloudflare tunnel without a converge, run on the host by the dashboard. Takes no secret: it authenticates as the Cloudflare token already in the store |
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
   **Bootstrap**: run by hand, over SSH, from outside. The set is small and is
   meant to stay small.
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
| `catena_admin_host` | The trust path the dashboard's dispatch arrives over: the runner account, the sudoers drop-in, the forced command and its `authorized_keys`. A reconcile that could rewrite the way in could rewrite what a reconcile is |
| `ansible_runtime` | The pinned ansible-core the host reconciles itself with. Also performed by the reconcile lane, deliberately: a root process with python3 can repair a broken runtime, so a host whose runtime broke does not need a human |

### Reconcile roles

Order is the converge order, which is dependency order.

| Role | Contract |
| --- | --- |
| `public_ports` | The declarative public-port registry and its applier |
| `payload` | Installs the host engine binaries, the lane scripts and their units, restic and rclone, by extracting them from the running dashboard image. Deploys no container |
| `host_maintenance` | The OS update configuration: unattended-upgrades and its origins (Debian security, stable updates and point releases, and the private-network client's own repository), the restart policy for services left on replaced libraries, and apt's settings. Also the baseline packages, the journal's storage, the host resolving its own name, the host mail transfer agent kept off port 25, the time zone and locale set in the dashboard, and the reboot-required probe's configuration. The probe and its hourly timer ship in the payload |
| `swarm` | The swarm's own settings (task history, the data-node label), which a restore does not bring back, and the self-heal that restarts containers which lost the race to the overlay at daemon start |
| `traefik` | The reverse proxy and the `catena-network` overlay |
| `postgres` | The infrastructure Postgres swarm service, hosting the sign-on database |
| `portainer` | The container control plane |
| `cloudflare_tunnel` | The cloudflared swarm service, the tunnel and wildcard DNS. Self-defers when the store holds no Cloudflare token |
| `keycloak` | The sign-on identity provider and its realm |
| `oauth2_proxy` | One auth proxy per gated upstream |
| `infrastructure` | Gatus, Healthchecks, Beszel and its agent, the antivirus watch, the mail canary, the application wiring scripts, and the configuration of the dashboard and Gatus sync lanes, whose scripts and timers ship in the payload |
| `catena-admin` | The action catalogue, the bind-mount targets, the update lanes' per-host configuration and the list of services they may update, the dashboard's port declarations, and the dashboard container, created directly against the local swarm. It holds the key that drives Portainer, so it cannot depend on Portainer to run |
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
| An own-playbook role | `cloudflare_tunnel_regenerate` | Deletes this host's tunnel before handing back to `cloudflare_tunnel` to mint a new one. A converge able to do that would drop the public edge every run, so the delete is only ever an explicit request: the dashboard's Regenerate the tunnel, which runs `rotate-tunnel.yml` |

### Paths a reconcile task may never write

`/etc/ssh/`, `/etc/sudoers.d/`, `authorized_keys`,
`/usr/local/sbin/catena-admin-runner`, `/etc/sysctl.d/`, `/etc/modprobe.d/`.

These are bootstrap-owned. A reconcile that can rewrite the way in can rewrite
what a reconcile is, so the rule is a list of literals that can be checked
rather than remembered.

Two qualifiers, both deliberate. The rule covers **task files only**: a
defaults file may name `/etc/ssh` because restic reads it into the backup set,
and reading a path is not writing it. And comments are stripped before
matching, so the file that explains why a reconcile must not write the forced
command is allowed to say so.

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

## State the tasks produce

Six things outlive the run that wrote them. Each belongs to the role or task
that produces it.

### The on-box store -- `common`, read by every role

`/etc/catena/config.json`, 0600 root, is the runtime source of truth: the
admin password an install supplies, every setting and credential saved in the
dashboard, and every internal secret, minted on the host. It rides the backup.
The inventory is bootstrap input only. `ansible/helpers/onbox_config.py`
declares which names are secrets. Every converge loads the store first, so a
tag-filtered run reads the same values as a full one.

| Invariant | Enforced by |
| --- | --- |
| The store is 0600 root, holds the full secret set, and rides the backup | `bench:ce_install_suite`, `bench:recover_secrets_from_running_host`, `threat:CV3` |
| A tag-filtered converge reads the store, not a stale inventory value | `bench:converge_suite#stage-7c-rotated-secret-arrives` |

### The public-port registry -- `public_ports`

A port is declared before it opens: infrastructure roles drop a JSON fragment
into `/etc/catena/public-ports.d/`, application templates carry
`vps.expose.*` compose labels. `catena-public-ports.py` merges both into ufw
and DOCKER-USER rules, and validation and the external scan check against the
merged set.

ufw is default-deny. No web port is bound on the host: web traffic enters
through the Cloudflare tunnel. The one exception is the TURN relay's UDP media
plane on the public IP, present only while a consumer runs.

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
| SSH is key-only with no root login; public 22 closes only behind a proven private network, reopens while that network is down, and opens again when no private network is applied | `bench:security_scan`, `bench:fi_n8_ufw_concurrent_ssh`, `bench:ce_install_suite`, `threat:CV6` |
| A server installs with no tunnel credential and brings its public edge up later without a reinstall | `bench:ce_install_suite` |
| A credential entered in the dashboard is checked on the host before it is stored, and a refused one is never stored | `bench:ce_install_suite`, `bench:fi_s4_tailscale_oauth_revoked` |
| The tunnel can be replaced on a live host without a converge | `bench:cf_tunnel_regenerate_round_trip` |
| Each domain token grants exactly one domain; Community caps at one | `bench:fi_n10_multidomain_cap` |
| The private-network control server is pluggable | `bench:ce_install_headscale` |

### The restic repository -- `backup`

Backups are encrypted on the host and go to an S3 repository set in the
dashboard. The restic password is generated on the host once the repository
and its keys are saved, and shown once: the host keeps it to run backups, and
the copy kept off the host opens the repository once the host is gone. A
server rebuilds from the endpoint and that keyset alone.

restic and rclone ship in the payload, pinned by digest. The backup wrapper
creates the repository when it finds none, and stops when the password is
wrong. Snapshots list, browse and export without a restore.

Restores run on the host through `catena-recovery`, the payload's restore
engine, which the dashboard's restore page drives. They need no converge
afterwards. Progress is recorded in `/var/lib/catena/recovery.state`: an
interrupted restore resumes with the same snapshot and scope, refuses a
different one, and only a finished run clears the record.

| Invariant | Enforced by |
| --- | --- |
| A server rebuilds from only the backup endpoint and key | `bench:dr_suite#stage-19-onbox-store`, `bench:ce_restore`, `bench:recover_secrets_from_running_host`, `threat:CV9` |
| Backups are encrypted on the host before upload, under a password also kept off the host | `bench:dr_suite#stage-13-disaster-recovery`, `bench:ce_restore`, `threat:CV4` |
| Backups restore -- rehearsed, not assumed | `bench:backup_rollback`, `bench:ce_restore` |
| An interrupted restore resumes with the same snapshot and scope, and is never reported as finished | `bench:fi_n5_provider_outage_mid_restore`, `bench:inplace_restore_suite#app-restore-stage-6-halted-scope-refuses-to-widen` |
| Snapshots export without a restore | `bench:snapshot_export_round_trip` |
| The backup key rotates without losing the repository | `bench:restic_password_rotation_round_trip` |
| Deleting the dashboard costs convenience, never data: backups run, restores work, applications stay online | `bench:sovereign_exit`, `bench:recovery_readme_manual_restore` |

### The timers -- `public_ports`, `infrastructure`, `host_maintenance`

Scheduled work is default-deny. This repository ships three local-maintenance
timers (the public-port reconcile, the antivirus watch, the mail canary) and
enables three the payload ships (the dashboard and Gatus syncs, the hourly
reboot-required probe). None spends object storage.

Every lane (backup, the bit-rot check, the offsite copy, the daily update
chain, dashboard updates, the scheduled converge) ships in the payload with its
timer off. `catena-schedule apply` turns a lane on only on a licensed host, at
the cadence set in the dashboard. The backup, the updates, the converge, the
container engine upgrade and the restart stay runnable by hand on every host.

The daily chain applies updates whether or not a backup is configured, and the
dashboard warns on every page for as long as none is. A configured backup that
fails, or fails its verification, stops the chain before any update. After the
container updates, the chain upgrades the container engine to the version
pinned in this repository, within the major version it runs, and restores the
previous version when the host does not come back healthy. Its last step
restarts the host when an update needs it, and checks afterwards that every
service came back.

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
| A failed backup verification stops the daily chain before any update | `bench:daily_chain_verify_hot_fail_aborts_updates` |

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
applied, as `git describe`: from the checkout on a converge started from a
controller, and from the provenance the dashboard image records on a converge
the host runs itself. It is the first thing either path writes, and it rides
the backup, so every snapshot names the version that produced its data.

A restore compares the snapshot's stamp with the host's: a newer snapshot is
refused, because a newer database directory does not open under an older
database; an older one needs an explicit upgrade; two stamps that cannot be
ordered are refused. A move between two servers is refused unless both run
the same version.

## Applications

The dashboard deploys applications from a catalogue resolved per host: domain
names, sign-on, and a password per application generated on the host. Per-app
compose lives in `catena-templates`; this repository owns the wiring that makes
the suite one product: mail, chat and video calling, file and office
integration, the antivirus watch and delivery canaries.

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

## CI

| Workflow | Jobs | Gates |
| --- | --- | --- |
| `ci.yml` | `installer`, `entry-points`, `syntax`, `duplication`, `prose` | the Python package and its unit tests, the two entry points a client runs (`catena-cli`, `catena-gui`) with their locks and the graphical installer's tests, Ansible syntax, copy-paste against a ratchet threshold, and comment prose |
| `security.yml` | `scanctl` | secrets, dependency vulnerabilities, static analysis, and the container images pinned in role defaults. A finding in this tree fails every run; a CVE in a pinned image fails the change that introduces it. A pin that stops matching fails the scan |

Every workflow runs on each pull request and on each push, except pushes to
the Renovate and engine-bump branches, whose pull requests test every push.

Images pinned in role defaults move through the catena-admin update engine,
run from the [renovate](https://github.com/catenahq/renovate) repository's
engine-bump workflow (seven-day soak, CVE gate, upgrade stops), one pull
request per defaults file. Every other dependency follows
[renovate](https://github.com/catenahq/renovate) and
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
| Dependencies and pinned images are vulnerability-gated | `scanctl:osv-scanner`, `scanctl:govulncheck`, `scanctl:trivy`, `workflow:security.yml#scanctl` |
| Static analysis runs on every change | `scanctl:semgrep`, `scanctl:zizmor` |
| The installer builds, lints and tests green | `workflow:ci.yml#installer`, `workflow:ci.yml#entry-points`, `workflow:ci.yml#syntax`, `workflow:ci.yml#duplication` |
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

- **The version stamp from both converge paths.** Only a converge started from
  a controller writes it today, so a host that updated itself keeps the stamp
  of its install, and its snapshots name that version rather than the one it
  runs.
- **A dashboard update applies its own converge,** and rolls the dashboard back
  when that converge fails. The scheduled converge lane does not exist yet.
- **Updates without a backup.** Today the daily chain pauses them unless the
  dashboard allows it; the warning shown on every page does not exist yet.
- **The pending-restart notice on every page.** Today it is shown on the
  System page only.
- **The container engine pin, its hold, and its upgrade** by hand and from the
  daily chain, with rollback, and its release on uninstall. Today the engine
  installs unpinned and moves on every converge started from a controller.
- **The daily chain's restart step.**
- **The unattended-upgrades origins beyond the security suite,** and the
  restart policy for services left on replaced libraries.
- **The role moves.** The `swarm` role; `host_maintenance` taking the OS update
  configuration, the baseline packages, the journal's storage, name resolution
  and the mail transfer agent from `common`, and running after `payload`;
  `catena-admin` taking the update lanes' configuration and the dashboard's
  port declarations from `catena_admin_host`.

## Installation

See [README.md](README.md).
