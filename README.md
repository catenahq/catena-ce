# catena-ce

The public, fair-code base of Catena: a self-hostable business suite you
own. Run the **Community** edition yourself for free -- the full app
catalog plus the base lifecycle (install, single sign-on, monitoring
basics, a single backup runner, and restore/recovery). Subscribe to the
managed **Business** edition for the parts Community does not include:
offsite immutable backups, the automation engine (managed updates, CVE
response), monitoring, identity governance, and a monthly assurance
report -- with us operating the whole lifecycle for you.

This repository will hold:

- **catena-admin (Go shell)** -- the community admin surface: a single
  binary hosting Community panels/actions and, when a Business license
  validates, the license-gated enterprise plugins pulled from
  `catenahq/catena-ee`. One image; EE rides in as downloaded
  plugin binaries gated at runtime (no second build).
- **Base automation** -- the `preflight` / `bootstrap` / `site` /
  `validate` / `restore` flows + shared roles + the single-backup runner.
  (Migrating from `catenahq/ops`.)
- **Installer / CLI** -- a thin entry point so self-hosters never touch
  raw Ansible.

Enterprise (Business) code is NOT here: it lives privately in
`catenahq/catena-ee` and ships as compiled, license-gated
binaries. See [LICENSE](LICENSE).

## Layout (so far)

```
cmd/catena-admin/      the Go shell entry point
internal/license/      ed25519 license-token validation (offline + grace)
internal/plugin/       the CE/EE plugin registry seam
```

## Develop

Requires Go 1.26+.

```
go build ./...
go vet ./...
go test ./...
```

### Run the shell

```
# Community-only (no license):
go run ./cmd/catena-admin
#   -> serves /healthz and /licensez on :8080

# With a Business license (token + operator public key):
CATENA_LICENSE="<token>" CATENA_LICENSE_PUBKEY="<base64-ed25519>" \
  go run ./cmd/catena-admin
```

`CATENA_ADMIN_ADDR` overrides the listen address. With no (or an invalid)
license the shell runs Community-only; it never fails closed on a missing
key.

## Editions

| Edition | What it is | Price |
| --- | --- | --- |
| Community | Self-host the full app catalog plus the base lifecycle (install, SSO, monitoring basics, single backup, restore/recover). Source-available, no telemetry. Offsite immutable backups and the automation engine are NOT included -- but recovery is, so you can always read and restore from a cold backup. | Free |
| Business | Everything in Community, operated for you, plus the parts Community does not include: offsite immutable backups (write), the automation engine (managed updates, CVE response), monitoring, identity governance, monthly assurance report. | Managed subscription |
| Bespoke | Design a suite around your workflow. | Quoted |
