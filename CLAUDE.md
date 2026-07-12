# catena-ce (public fair-code base)

The public, fair-code base of Catena. Sibling repo under the workspace at
`ma-lalonde.dev/catena/`; tracks `git@github.com:catenahq/catena-ce.git` on
`main`. Commit on `main`, no per-feature branches (workspace convention).

## What lives here vs not

- **Here (public, fair-code):** the catena-admin **Go shell** (CE panels +
  actions + the plugin seam + license validation), the base Ansible
  (preflight/bootstrap/site/validate/restore + shared roles + single
  backup), and the installer/CLI. See [LICENSE](LICENSE).
- **NOT here:** enterprise (Business) code. It is private in
  `catenahq/catena-ee` and ships as compiled, license-gated
  plugin binaries. Never paste EE source or operational/cross-host
  playbooks into this repo.
- **Migration in progress:** code is moving out of `catenahq/ops`. `ops`
  keeps the test bench + dev tooling and sheds what moves here (no
  duplicates).

## Go

- Module: `github.com/catenahq/catena-ce`. Go 1.26+.
- `go build ./... && go vet ./... && go test ./...` must stay green.
- `license` (public package) is the single definition of the license-token
  wire format (ed25519, offline verify, grace window). It is public so
  catena-ee can import it: the license endpoint there mints tokens with the
  matching private key via `Sign`, and EE plugins reference `license.Edition`.
  Keep `Sign`/`Verify` in lockstep if the format changes.
- `plugin` (public SDK) is the CE/EE contract enterprise plugins implement;
  it is outside `internal/` on purpose so the catena-ee module can import
  it. `internal/registry` is the host-side store that gates Business
  plugins on an active license. The shell only ever sees `plugin.Plugin`.
- The shell must run Community-only when no/invalid license is present; it
  never fails closed on a missing key.

## Discipline

- No secrets in the repo. The operator public key is supplied at runtime
  (`CATENA_LICENSE_PUBKEY`), never committed as a private key.
- Match the surrounding Go style; keep packages small and tested.

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
- No EE logic, no cross-host flows, license VERIFY side only (CP2/CP3)
  -- `audit --check-port` gates it.
- Admin surfaces stay behind the identity gate + in-app role check
  (CV2); the shell never fails closed on a missing license (LE3).
- Secret-bearing Ansible tasks carry `no_log: true` (CV7 -- currently
  review-enforced; lint gate is backlogged).
- SPEC.md invariant pointers must resolve (`audit --check-public-specs`);
  editing a scenario/workflow/gate name breaks the public sheet that
  cites it -- fix the pointer in the same change.
