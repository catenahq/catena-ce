# Catena Community -- Specification

This file states what this repository promises. It is hand-written and
deliberately short; every claim in the invariants table below points at
a machine-checked gate, and the maintainers' CI resolves each pointer
on every change -- a claim whose gate disappears fails the build. The
generated companion sheet [VALIDATION.md](VALIDATION.md) lists what is
tested and how much.

## Intent

Catena Community is the published, fair-code base of Catena: the
installer/CLI and the automation that prepares a server and deploys the
application suite. It is built for one north star:

> **A Catena server can be rebuilt from nothing but its backup storage
> endpoint and the backup key.**

Everything else -- configuration on the server, encrypted backups to
client-owned storage, whole-server recovery -- serves that promise.

## Boundaries

- **Community vs Pro.** This repository is complete and functional on
  its own, including a proven scheduled-backup lane: one weekly backup
  timer, rate-limited to weekly-or-sparser cadence at converge time.
  Catena Pro (private repository) adds the catena-admin web panel and
  the licensed automation on top: daily and sub-daily backups, managed
  updates, daily maintenance, offsite immutable backup copies,
  attestation. The panel is a convenience layer over the host-native
  automation in THIS repository -- every operation it drives (backup,
  restore, validate, converge) runs here without it. Scheduled work in
  Community is **default-deny** -- the weekly backup plus an enumerated
  set of local maintenance watchdogs, machine-enforced, not
  conventional. Pro also unlocks **multiple domains**, each served as its
  own sign-on island (a per-domain identity issuer + session cookie, so
  users of one domain never see another's login); Community serves a
  single domain.
- **Secrets.** The repository contains none. The secrets a deployment
  needs are held by the server owner, documented in
  [ansible/SECRETS.md](ansible/SECRETS.md).
- **Exposure.** A deployed server accepts no inbound web traffic on its
  own ports. With the encrypted tunnel configured, all web traffic
  arrives through it and remote administration rides a private network.
  The tunnel is optional: without it the server runs in **private-network
  access mode**, where the applications are reached over the private
  network by port -- no public web edge at all.
- **Private network.** The control server for the private network is
  pluggable: a hosted control plane by default, or a self-hosted one the
  operator points the server at with a URL and a join credential.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| A server rebuilds from only the backup endpoint + key | `bench:restore_dr`, `bench:ce_restore`, `bench:recover_secrets_from_running_host` |
| Re-running the installer converges; it never duplicates or breaks a healthy server | `bench:ce_converge`, `bench:converge_modify` |
| No web port is open on the server itself; an external scan proves it | `bench:security_scan`, `bench:fi_v2_external_scan_blocked` |
| Validation checks both directions: services answer, forbidden exposure does not | `bench:ce_validate` |
| The tunnel is optional: in private-network access mode the server stands up with no public edge | `bench:ce_install_tailnet` |
| Each domain token grants exactly one domain; Community caps at one, more is a Pro boundary | `bench:fi_n10_multidomain_cap` |
| The private-network control server is pluggable: a node joins a self-hosted control server, not only the hosted one | `bench:ce_install_headscale` |
| Backups restore -- rehearsed, not assumed | `bench:backup_rollback`, `bench:hot_restore_round_trip` |
| The scheduled backup is rate-limited to weekly; tighter cadence fails the converge | `bench:backup_tier_schedule_resolution` |
| Scheduled work is default-deny: weekly backup + enumerated maintenance timers only (Pro boundary) | `audit:check-port` |
| Every file and scenario is classified against the feature manifest; nothing untracked | `audit:check-grid`, `audit:check-all` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis) | `workflow:security.yml`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:govulncheck` |
| Pinned container images are CVE-gated | `workflow:trivy.yml` |
| The Python installer builds and tests green | `workflow:ci.yml` |
| The admin panel image is signed, and its component inventory is published with it, so you can verify what you are about to run without asking us | `threat:LE8` |
| The image that ships is the image that was scanned, and the moving tag never points at an unscanned one | `threat:LE9` |
| You can tell which build is running on your server, from the server | `threat:LE9`, `bench:ce_admin_smoke` |
| A licence problem never costs you access to your data, your backups, or a restore | `threat:LE3`, `bench:ee_lapse`, `bench:ee_ce_regression` |
| The record of administrative actions on your server can be exported and checked by someone who does not trust us: an edited or shortened trail fails verification | `threat:CV10`, `bench:audit_chain_tamper_evident` |

Gate pointer grammar: `bench:<scenario>` = a rehearsal scenario that
provisions disposable virtual machines and drives the real product;
`audit:<gate>` = a static gate of the maintainers' audit graph, run in
CI and before every rehearsal; `workflow:<file>` = a CI workflow in
this repository; `scanctl:<tool>` = a scanner run by the bundled
security workflow; `threat:<ID>` = a numbered invariant in the
maintainers' threat-model register, resolved by the same CI gate (the
register is operator-internal, but an ID that does not exist -- or one
that was retired -- fails the build, so a claim here cannot outlive the
guarantee behind it).

The admin panel's source is not published. Its binary is not obfuscated:
[verify what you run](https://docs.catena.run/en/trust/verify-what-you-run/)
walks through checking the signature, reading the component inventory,
and scanning the image yourself. The signature and inventory arrive with
the first release cut from the current pipeline.

## How this stays honest

The maintainers' CI regenerates [VALIDATION.md](VALIDATION.md) from the
audit manifest and fails on drift, resolves every pointer above, and
refuses unclassified artifacts. This file changes only when the product
promise changes.
