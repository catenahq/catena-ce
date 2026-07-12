# Catena Community -- Specification

This file states what this repository promises. It is hand-written and
deliberately short; every claim in the invariants table below points at
a machine-checked gate, and the maintainers' CI resolves each pointer
on every change -- a claim whose gate disappears fails the build. The
generated companion sheet [VALIDATION.md](VALIDATION.md) lists what is
tested and how much.

## Intent

Catena Community is the published, fair-code base of Catena: the
installer/CLI, the automation that prepares a server and deploys the
application suite, and the catena-admin dashboard. It is built for one
north star:

> **A Catena server can be rebuilt from nothing but its backup storage
> endpoint and the backup key.**

Everything else -- configuration on the server, encrypted backups to
client-owned storage, whole-server recovery -- serves that promise.

## Boundaries

- **Community vs Pro.** This repository is complete and functional on
  its own, including a proven scheduled-backup lane: one weekly backup
  timer, rate-limited to weekly-or-sparser cadence at converge time.
  Catena Pro (private repository) adds the licensed automation on top:
  daily and sub-daily backups, managed updates, daily maintenance,
  offsite immutable backup copies, attestation. Scheduled work in
  Community is **default-deny** -- the weekly backup plus an enumerated
  set of local maintenance watchdogs, machine-enforced, not
  conventional.
- **Secrets.** The repository contains none. The secrets a deployment
  needs are held by the server owner, documented in
  [ansible/SECRETS.md](ansible/SECRETS.md).
- **Exposure.** A deployed server accepts no inbound web traffic on its
  own ports; all web traffic arrives through an encrypted tunnel, and
  remote administration rides a private network.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| A server rebuilds from only the backup endpoint + key | `bench:restore_dr`, `bench:ce_restore`, `bench:recover_secrets_from_running_host` |
| Re-running the installer converges; it never duplicates or breaks a healthy server | `bench:ce_converge`, `bench:converge_modify` |
| No web port is open on the server itself; an external scan proves it | `bench:security_scan`, `bench:fi_v2_external_scan_blocked` |
| Validation checks both directions: services answer, forbidden exposure does not | `bench:ce_validate` |
| Backups restore -- rehearsed, not assumed | `bench:backup_rollback`, `bench:hot_restore_round_trip` |
| The scheduled backup is rate-limited to weekly; tighter cadence fails the converge | `bench:backup_tier_schedule_resolution` |
| Scheduled work is default-deny: weekly backup + enumerated maintenance timers only (Pro boundary) | `audit:check-port` |
| Every file and scenario is classified against the feature manifest; nothing untracked | `audit:check-grid`, `audit:check-all` |
| The Pro plugin seam honors its compile-time contract | `audit:check-go-plugins` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis) | `workflow:security.yml`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:govulncheck` |
| Shipped container images are CVE-gated | `workflow:trivy.yml` |
| The Go shell and the Python installer build, vet and test green | `workflow:ci.yml` |

Gate pointer grammar: `bench:<scenario>` = a rehearsal scenario that
provisions disposable virtual machines and drives the real product;
`audit:<gate>` = a static gate of the maintainers' audit graph, run in
CI and before every rehearsal; `workflow:<file>` = a CI workflow in
this repository; `scanctl:<tool>` = a scanner run by the bundled
security workflow.

## How this stays honest

The maintainers' CI regenerates [VALIDATION.md](VALIDATION.md) from the
audit manifest and fails on drift, resolves every pointer above, and
refuses unclassified artifacts. This file changes only when the product
promise changes.
