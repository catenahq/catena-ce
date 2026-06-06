# catena (public fair-code base)

The public, fair-code base of Catena. Sibling repo under the workspace at
`ma-lalonde.dev/catena/`; tracks `git@github.com:catenahq/catena.git` on
`main`. Commit on `main`, no per-feature branches (workspace convention).

## What lives here vs not

- **Here (public, fair-code):** the catena-admin **Go shell** (CE panels +
  actions + the plugin seam + license validation), the base Ansible
  (preflight/bootstrap/site/validate/restore + shared roles + single
  backup), and the installer/CLI. See [LICENSE.md](LICENSE.md).
- **NOT here:** enterprise (Business) code. It is private in
  `catenahq/catena-enterprise` and ships as compiled, license-gated
  plugin binaries. Never paste EE source or operational/cross-host
  playbooks into this repo.
- **Migration in progress:** code is moving out of `catenahq/ops`. `ops`
  keeps the test bench + dev tooling and sheds what moves here (no
  duplicates).

## Go

- Module: `github.com/catenahq/catena`. Go 1.26+.
- `go build ./... && go vet ./... && go test ./...` must stay green.
- `internal/license` is the single definition of the license-token wire
  format (ed25519, offline verify, grace window). The license endpoint in
  catena-enterprise mints tokens with the matching private key -- keep
  `Sign`/`Verify` in lockstep if the format changes.
- `internal/plugin` is the CE/EE seam. Community plugins always enabled;
  Business plugins enabled only while a license is `Active`. The shell
  only ever sees the `Plugin` interface.
- The shell must run Community-only when no/invalid license is present; it
  never fails closed on a missing key.

## Discipline

- No secrets in the repo. The operator public key is supplied at runtime
  (`CATENA_LICENSE_PUBKEY`), never committed as a private key.
- Match the surrounding Go style; keep packages small and tested.
