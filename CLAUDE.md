# catena-ce (public fair-code base)

The public, fair-code base of Catena. Sibling repo under the workspace at
`ma-lalonde.dev/catena/`; tracks `git@github.com:catenahq/catena-ce.git` on
`main`. Commit on the checked-out branch (currently `dev`), no per-feature
branches (workspace convention). NEVER switch or fast-forward `main`.

## What lives here vs not

- **Here (public, fair-code):** the base Ansible (preflight/bootstrap/site/
  validate/restore + shared roles + single backup), the installer/CLI, the
  catena-admin container compose (deploy/catena-admin/), and the public
  `license` Go package. See [LICENSE](LICENSE).
- **NOT here:** the catena-admin shell and all Business code. Since the
  2026-07 unification they live in the private `catenahq/catena-admin`
  repo (formerly catena-ee) and ship as ONE private GHCR image with every
  panel compiled in. Never paste private source or operational/cross-host
  playbooks into this repo.

## Go

- Module: `github.com/catenahq/catena-ce`. Go 1.26+. The only Go here is
  `license/`.
- `go build ./... && go vet ./... && go test ./...` must stay green.
- `license` (public package) is the single definition of the license-token
  wire format (ed25519, offline verify, grace window). It is public so the
  private catena-admin module can import it: the mint CLI there signs
  tokens with the matching private key via `Sign`, and the shell verifies
  offline. Keep `Sign`/`Verify` in lockstep if the format changes. The
  verify side is public ON PURPOSE (auditable check; CP2/CP3).
- The shell must run Community-only when no/invalid license is present; it
  never fails closed on a missing key (LE3 -- enforced in the private repo,
  asserted by bench ee_lapse/ee_ce_regression).

## Discipline

- No secrets in the repo. The operator public key is supplied at runtime
  (`CATENA_LICENSE_PUBKEY`), never committed as a private key.
- Match the surrounding style; keep the installer + roles idempotent and
  atomic (see ops CLAUDE.md conventions).

## Security invariants (machine-enforced -- do not weaken silently)

Full register with stable IDs: ops
`internal_docs/operator/threat-models/{client-vps,ce-public-repo}.md`.
The load-bearing ones when editing THIS repo:

- No secret in tree or history (CP1) -- gitleaks gate on every change.
- Scheduled work is default-deny: ONE weekly backup timer (capped by
  the `backup_weekly_cap` filter; a tighter OnCalendar fails the
  converge) plus the enumerated maintenance timers in the audit's
  CE_ALLOWED_TIMERS (CV8). A new `.timer` needs a deliberate allowlist
  entry, and daily/sub-daily cadence is EE.
- No Business logic, no cross-host flows, license VERIFY side only
  (CP2/CP3) -- `audit --check-port` gates it.
- Admin surfaces stay behind the identity gate + in-app role check (CV2).
- Secret-bearing Ansible tasks carry `no_log: true` (CV7 -- currently
  review-enforced; lint gate is backlogged).
- SPEC.md invariant pointers must resolve (`audit --check-public-specs`);
  editing a scenario/workflow/gate name breaks the public sheet that
  cites it -- fix the pointer in the same change.
