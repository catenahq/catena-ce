# SPEC.md

Catena-ce is an Ansible-based installer for Catena. It prepares a new Debian 13 server to host dockerized applications by providing a secure, ready-to-deploy platform. The main dashboard, `catena-admin`, is built from a closed-source repository and its image is available [on Github](https://github.com/catenahq/catena-admin/pkgs/container/catena-admin)

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

This repository is complete and functional on its own, including a proven
on-demand backup and restore. Catena Pro adds licensed automation on top of the
same host-native operations: scheduled backups, daily and sub-daily, managed
updates with rollback, daily maintenance, offsite immutable copies,
attestation, central audit shipping, multiple sign-on domains, and
server-to-server moves.

Every operation the dashboard drives runs on the host without it: install,
converge, validate and backup from this repository, and restore through the
`catena-recovery` binary the payload installs.

## How this repository is layered

A desired state is reached in four layers, widest to narrowest.

| Layer | What it is |
| --- | --- |
| **CLI** | `uv run catena <verb> --inventory <inventory>`. Selects a playbook, or chains several. Prompts for what it needs, and never asks Ansible anything the product cannot answer for itself |
| **Playbook** | Names the hosts and the ordered list of roles to apply to them. One playbook is one atomic operation |
| **Role** | A unit of related tasks plus its defaults, templates, handlers and its own `validate.yml`. The reuse boundary: several playbooks apply the same role |
| **Task** | One step. Task files are shared between roles and between playbooks where the step is the same step |

Reuse happens downward only. A role never calls a playbook, and a task never
decides which role it belongs to.

## CLI

The entry point is the `catena` console script declared in
`ansible/pyproject.toml`, implemented in `ansible/catena_cli.py`, and run from
`ansible/`:

```sh
uv run catena <verb> --inventory <inventory>
```

A verb that runs one playbook carries that playbook's name. A verb that chains
several does not, because there is no single playbook to name it after.

### Verbs that run one playbook

| Verb | Playbook |
| --- | --- |
| `converge` | `converge.yml` |
| `validate` | `validate.yml` |
| `backup` | `backup.yml` |
| `rotate-tunnel` | `rotate-tunnel.yml` |
| `rotate-tailscale` | `rotate-tailscale.yml` |
| `show-keyset` | `show-keyset.yml` |
| `uninstall` | `uninstall.yml` |

### Verbs that chain playbooks

| Verb | Chain | For |
| --- | --- | --- |
| `install` | seed, then `preflight`, `bootstrap`, `converge`, `lockdown`, `validate` | A fresh server |

`preflight` runs as its own invocation ahead of the chain, so a stray
`--limit` cannot skip it.

### seed

`ansible/seed.py` is not a playbook. It is a Python step the CLI runs before
Ansible starts, on `install` only: it writes the inventory and mints the
initial secret set. It has no verb of its own because seeding a server that is
already seeded is not an operation.

| Invariant | Enforced by |
| --- | --- |
| The bundled CLI is the only supported entry point; `ansible-playbook` is not one | `bench:ce_install_suite`, `bench:ce_converge`, `bench:ce_validate` |
| A verb that runs one playbook is named after it | `audit:check-grid` |
| Every chain runs preflight before it touches a server | `bench:ce_install_suite` |

## Playbooks

| Playbook | What it is |
| --- | --- |
| `preflight.yml` | Controller-side. Proves the supplied Tailscale OAuth client works before any server is touched |
| `bootstrap.yml` | The half that cannot self-repair. Run by hand, over SSH, from outside |
| `converge.yml` | The full converge. Safe to re-run |
| `lockdown.yml` | Closes public SSH behind the access method the host declared, or reports why it stays open. The install chain runs it after the converge, and the dashboard's SSH toggle runs the same playbook |
| `reconcile.yml` | The half a host runs against itself, from the dashboard image, with no controller inventory. Has no verb: nothing on a controller dispatches it |
| `validate.yml` | Three vantages: on-host per-role checks, tailnet from the controller, external from the controller |
| `backup.yml` | On-demand snapshot |
| `show-keyset.yml` | Re-display the disaster-recovery keyset |
| `rotate-tunnel.yml` | Replace the host's Cloudflare tunnel without a converge. Takes no secret: both halves of the rotation authenticate as the Cloudflare token already in the store |
| `rotate-tailscale.yml` | Force re-authentication to the mesh |
| `uninstall.yml` | Hand the OS update lane back to Debian and print teardown guidance |

`converge.yml` and `reconcile.yml` apply the same roles in the same order.
`reconcile.yml` is `converge.yml` minus the roles a human must run and minus
the steps that only make sense on a fresh box. Which roles that leaves is
declared, not decided at the moment of extraction.

| Invariant | Enforced by |
| --- | --- |
| Re-running a converge repairs a drifted server; it never duplicates or breaks a healthy one | `bench:ce_converge`, `bench:converge_suite#stage-7c-rotated-secret-arrives`, `bench:converge_suite#stage-7b-bump-survives` |
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
| `common` | The OS baseline and the ufw lockdown. Sets up the account and the packages every later role assumes, and closes public SSH at the end of an install |
| `host_hardening` | Kernel and module hardening, applied before dockerd's first start so its runtime writes do not win until the next reboot. Re-applying it under a live workload is not a thing to do unattended |
| `tailscale` | The mesh the host is reached over. Getting it wrong is the definition of question one |
| `storage` | Mounts the block volume every service keeps its data on. A failure here costs data rather than access, and it cannot be safely re-applied to a live host by a timer |
| `docker` | The engine and the swarm. Nothing else runs without it, and that includes whatever would have repaired it |
| `catena_admin_host` | The trust path the dashboard's dispatch arrives over: the runner account, the sudoers drop-in, the forced command and its `authorized_keys`. A reconcile that could rewrite the way in could rewrite what a reconcile is |
| `ansible_runtime` | The pinned ansible-core the host reconciles itself with. Also performed by the reconcile lane, deliberately: a root process with python3 can repair a broken runtime, so a host whose runtime broke does not need a human |

### Reconcile roles

Order is the converge order, which is dependency order.

| Role | Contract |
| --- | --- |
| `public_ports` | The declarative public-port registry and its applier |
| `payload` | Installs the host engine binaries, the lane scripts and their units, restic and rclone, by extracting them from the running dashboard image. Deploys no container |
| `traefik` | The reverse proxy and the `catena-network` overlay |
| `postgres` | The infrastructure Postgres swarm service, hosting the sign-on database |
| `portainer` | The container control plane |
| `cloudflare_tunnel` | The cloudflared swarm service, the tunnel and wildcard DNS. Self-defers when the store holds no Cloudflare token |
| `keycloak` | The sign-on identity provider and its realm |
| `oauth2_proxy` | One auth proxy per gated upstream |
| `infrastructure` | Gatus, Healthchecks, Beszel and its agent, the antivirus watch, the mail canary, the application wiring scripts, and the configuration of the dashboard and Gatus sync lanes, whose scripts and timers ship in the payload |
| `host_maintenance` | The reboot-required probe's configuration. The probe and its hourly timer ship in the payload |
| `catena-admin` | The action catalogue, the bind-mount targets and the dashboard container, created directly against the local swarm. It holds the key that drives Portainer, so it cannot depend on Portainer to run |
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
| An own-playbook role | `cloudflare_tunnel_regenerate` | Deletes this host's tunnel before handing back to `cloudflare_tunnel` to mint a new one. A converge able to do that would drop the public edge every run, so the delete is an intent somebody declares by running `rotate-tunnel.yml` |

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
| A restore is neither a CLI verb nor a converge step: it runs only when somebody starts one on the host | `workflow:ci.yml#installer`, `bench:ce_restore`, `bench:backup_rollback` |
| The reconcile half reads nothing from a controller inventory | `bench:payload_action_dispatches_without_converge` |

## State the tasks produce

Five things outlive the run that wrote them. Each belongs to the role that
produces it.

### The on-box store -- `common`, read by every role

`/etc/catena/config.json` is the runtime source of truth. Mode 0600 root. It
adopts the seeded external credentials, mints every internal service secret on
the host itself, and rides the restic backup. The inventory under
`ansible/inventory/<name>/` is plaintext bootstrap input, not the source of
truth. `ansible/helpers/onbox_config.py` declares which names are secrets.

Loaded first in every converge, so a tag-filtered run reads the same values a
full one does.

| Invariant | Enforced by |
| --- | --- |
| The store is 0600 root, holds the full secret set, and rides the backup | `bench:ce_install_suite`, `bench:recover_secrets_from_running_host`, `threat:CV3` |
| A tag-filtered converge reads the store, not a stale inventory value | `bench:converge_suite#stage-7c-rotated-secret-arrives` |

### The public-port registry -- `public_ports`

Anything that opens a port declares it first. Infrastructure roles drop a JSON
fragment into `/etc/catena/public-ports.d/`; application templates declare
theirs through `vps.expose.*` compose labels, harvested live.
`catena-public-ports.py` merges both and applies ufw and DOCKER-USER rules.
The merged effective set is what validation and the external scan check
against.

ufw is default-deny. After lockdown, ingress is tailnet-only and SSH is
key-only with no root login. Web traffic reaches the server through the
encrypted tunnel; no web port is bound on the host. The TURN relay is the one
deliberate exception: a UDP media plane bound direct on the public IP,
deployed only when a consumer is running.

The tunnel is **deferred, not required, at install**. A server stands up with
no Cloudflare credential, is administered over the private mesh, and brings
its public edge up later without a reinstall.

| Invariant | Enforced by |
| --- | --- |
| No web port is open on the server itself; an external scan proves it | `bench:security_scan`, `bench:fi_v2_external_scan_blocked`, `threat:CV1` |
| Every open port is declared before it is opened | `bench:security_scan`, `bench:ce_validate` |
| SSH is key-only and tailnet-only after bootstrap; no root login | `bench:security_scan`, `bench:fi_n8_ufw_concurrent_ssh`, `threat:CV6` |
| A server installs with no tunnel credential and brings its public edge up later without a reinstall | `bench:ce_install_suite`, `bench:ce_install_no_tailnet` |
| The tunnel can be replaced on a live host without a converge | `bench:cf_tunnel_regenerate_round_trip` |
| Each domain token grants exactly one domain; Community caps at one | `bench:fi_n10_multidomain_cap` |
| The private-network control server is pluggable | `bench:ce_install_headscale` |

### The restic repository -- `backup`

Backups go to object storage the client owns, encrypted on the host before
upload. The restic password is never minted on-box: it must not ride inside
the backup it decrypts. A whole server rebuilds from only its backup endpoint
and its keyset.

restic and rclone ship in the payload, pinned by digest. The backup wrapper
creates the repository the first time it finds none, and stops rather than
creating one when the password is wrong.

Snapshots can be listed, browsed and exported without a restore.

A restore runs on the host, from the dashboard's restore page or
`catena-recovery restore`, and needs no converge afterwards. It records its
progress in `/var/lib/catena/recovery.state`: a restore that stops part-way
stays on record, the next run resumes it and refuses a different snapshot or
set of applications, and only a finished run clears it.

| Invariant | Enforced by |
| --- | --- |
| A server rebuilds from only the backup endpoint and key | `bench:dr_suite#stage-19-onbox-store`, `bench:ce_restore`, `bench:recover_secrets_from_running_host`, `threat:CV9` |
| Backups are encrypted on the host before upload, and the key is never minted on-box | `bench:dr_suite#stage-13-disaster-recovery`, `bench:ce_restore`, `threat:CV4` |
| Backups restore -- rehearsed, not assumed | `bench:backup_rollback`, `bench:ce_restore` |
| An interrupted restore resumes with the same snapshot and scope, and is never reported as finished | `bench:fi_n5_provider_outage_mid_restore`, `bench:inplace_restore_suite#app-restore-stage-6-halted-scope-refuses-to-widen` |
| Snapshots export without a restore | `bench:snapshot_export_round_trip` |
| The backup key rotates without losing the repository | `bench:restic_password_rotation_round_trip` |
| Deleting the dashboard costs convenience, never data: backups run, restores work, applications stay online | `bench:sovereign_exit`, `bench:recovery_readme_manual_restore` |

### The timers -- `public_ports`, `infrastructure`, `host_maintenance`

Scheduled work is default-deny: an enumerated set, machine-enforced rather
than conventional. This repository ships three local-maintenance timers: the
public-port reconcile, the antivirus watch and the mail canary. It enables
three more that the payload ships and that run on every host: the dashboard
and Gatus syncs and the hourly reboot-required probe. None of them spends
object storage.

Every lane -- backup, the bit-rot check, the offsite copy, the daily chain
that runs container updates, and dashboard updates -- ships in the payload
with its timer off.
`catena-schedule apply` turns a lane on only on a licensed host, at the
cadence set on the dashboard's Schedules page. The backup, container-update
and dashboard-update services stay runnable by hand on every host.

Debian's own unattended-upgrades applies OS security patches.

| Invariant | Enforced by |
| --- | --- |
| Scheduled work is default-deny: this repository ships only enumerated local-maintenance timers, and every lane ships in the payload | `audit:check-port`, `threat:CV8` |
| An unlicensed host schedules no lane at all | `bench:unlicensed_schedules_nothing` |

### The release manifest -- written last, by both converge paths

Every converge records what it delivered, from `converge.yml` and from
`reconcile.yml` alike, so an on-host converge does not leave the fields
describing the last one a human ran. Before that, a prune removes files a
previous converge installed that this one no longer ships, provided the file
is still byte-for-byte what the converge wrote and the payload manifest
(`/var/lib/catena/payload-manifest.json`) does not claim it. A file that moves
from a role into the payload stays installed.

| Invariant | Enforced by |
| --- | --- |
| Every converge records what it delivered | `bench:converge_suite#stage-7a-the-action-is-back` |
| Withdrawn files are removed, and Community files are not pruned as though they were licensed | `bench:payload_prune_respects_ce` |
| A converge never prunes a file the payload claims | `workflow:ci.yml#installer` |

## Applications

The dashboard deploys applications from a catalogue resolved per host: its
domain names, its sign-on, and a fresh password for each application,
generated on the server itself. Per-app compose lives in `catena-templates`;
what this repository owns is the wiring that makes the suite behave as one
product -- mail, chat and video calling, file and office integration,
antivirus watch and delivery canaries.

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
| `ci.yml` | `installer`, `syntax`, `duplication`, `prose` | the Python package and its unit tests, Ansible syntax, copy-paste against a ratchet threshold, and comment prose |
| `security.yml` | `scanctl` | secrets, dependency vulnerabilities and static analysis |
| `trivy.yml` | `trivyignore-expiry`, `trivy-gate` | the container images pinned in role defaults. A pin that stops matching fails the scan |
| `seed-baseline.yml` | `seed` | the seeded configuration against its recorded baseline |

The container images pinned in role defaults move with the catena-admin update
engine, run from the [renovate](https://github.com/catenahq/renovate)
repository's engine-bump workflow: the decision a host makes for a running
service (a seven-day soak, a CVE gate, upgrade stops), one pull request per
defaults file, merged when this repository's required checks pass. Every other
dependency is kept current by
[renovate](https://github.com/catenahq/renovate) and
[renovate-config](https://github.com/catenahq/renovate-config), under a
seven-day cooldown, with GitHub Actions pinned by digest.
[scanctl](https://github.com/catenahq/scanctl) scans each update before it
merges.

| Invariant | Enforced by |
| --- | --- |
| No secret is in the tree or its history | `scanctl:gitleaks`, `workflow:security.yml#scanctl`, `threat:CP1` |
| No licensed source and no Go in this tree | `audit:check-port`, `audit:check-grid`, `threat:CP3` |
| Dependencies and pinned images are vulnerability-gated | `scanctl:osv-scanner`, `scanctl:govulncheck`, `workflow:trivy.yml#trivy-gate`, `workflow:trivy.yml#trivyignore-expiry` |
| Static analysis runs on every change | `scanctl:semgrep`, `scanctl:zizmor` |
| The installer builds, lints and tests green | `workflow:ci.yml#installer`, `workflow:ci.yml#syntax`, `workflow:ci.yml#duplication` |
| Code comments describe the tree as it stands, not its history | `workflow:ci.yml#prose` |
| The seeded configuration matches its baseline | `workflow:seed-baseline.yml#seed` |
| Public prose names no system Catena does not ship | `audit:check-banned-words` |
| Every file and scenario is classified against the feature manifest; nothing untracked | `audit:check-grid`, `audit:check-all` |
| This file cannot drift from reality | `audit:check-public-specs`, `threat:CP7` |

Gate pointer grammar: `bench:<scenario>` is a rehearsal scenario that
provisions disposable virtual machines and drives the real product;
`audit:<gate>` is a static gate of the maintainers' audit graph, run in CI and
before every rehearsal; `workflow:<file>#<job>` is a CI job in this
repository; `scanctl:<tool>` is a scanner run by the bundled security
workflow; `threat:<ID>` is a numbered invariant in the maintainers'
threat-model register, resolved by the same CI gate. A pointer that does not
resolve, or one that names a retired invariant, fails the build -- so no claim
here can outlive the guarantee behind it.

## Declared, not yet built

This file leads the code. Anything it declares that the tree does not
implement is listed here, so the gap is a finding rather than a silence.

Nothing outstanding.

## Installation

See [README.md](README.md).
