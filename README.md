# catena-ce

The public, fair-code base of Catena: a self-hostable business suite you
own. Run the **Community** edition yourself for free -- the full app
catalog plus the base lifecycle (install, single sign-on, monitoring
basics, a single backup runner, and restore/recovery). Subscribe to the
managed **Business** edition for the parts Community does not include:
offsite immutable backups, the automation engine (managed updates, CVE
response), monitoring, identity governance, and a monthly assurance
report -- with us operating the whole lifecycle for you.

This repository holds:

- **catena-admin (Go shell)** -- the community admin surface: a single
  binary hosting Community panels/actions and, when a Business license
  validates, the license-gated enterprise plugins pulled from
  `catenahq/catena-ee`. One image; EE rides in as downloaded
  plugin binaries gated at runtime (no second build).
- **Base automation** -- the `preflight` / `bootstrap` / `site` /
  `validate` / `restore` flows + shared roles + the single-backup runner.
- **Installer / CLI** -- `ansible/catena`, a thin entry point so
  self-hosters never touch raw Ansible (see [Install](#install-self-host)).

Enterprise (Business) code is NOT here: it lives privately in
`catenahq/catena-ee` and ships as compiled, license-gated
binaries. See [LICENSE](LICENSE).

## Layout

```
ansible/               the Community deploy automation + the CLI
  catena               the installer / CLI entry point (see ansible/README.md)
  playbooks/ roles/    preflight/bootstrap/site/validate/restore + shared roles
  seed.py              config + SOPS-vault seeding (first-run secret minting)
cmd/catena-admin/      the Go shell entry point
license/               ed25519 license-token wire format (offline verify + grace)
plugin/                the CE/EE plugin SDK contract (implemented by catena-ee)
internal/registry/     host-side store that gates Business plugins on a license
loader/                go-plugin loader for the downloaded EE plugin binaries
deploy/catena-admin/   the catena-admin container compose
```

## Install (self-host)

Self-hosters drive everything through the bundled **`catena` CLI** -- you
never call `ansible-playbook` directly. Prerequisites: `sops` and `age` on
PATH (ansible-core comes from `uv`). From the `ansible/` directory:

```
uv run ./catena install   --inventory prod   # seed + preflight -> bootstrap -> site -> validate
uv run ./catena converge  --inventory prod   # re-apply site.yml after a config / app-tag change
uv run ./catena validate  --inventory prod   # on-host + tailnet + external checks
uv run ./catena backup    --inventory prod   # trigger an on-demand snapshot (manual CE backup)
uv run ./catena restore   --inventory prod   # in-place whole-host restore
uv run ./catena recover   --inventory prod   # DR onto a FRESH replacement box
uv run ./catena rollback  --inventory prod   # roll a still-running host back to a snapshot
uv run ./catena rotate-tunnel    --inventory prod   # regenerate the Cloudflare tunnel
uv run ./catena rotate-tailscale --inventory prod   # re-auth the node to the tailnet
uv run ./catena uninstall --inventory prod   # hand unattended-upgrades back to the OS
```

`install` first runs `seed.py` (collects config, mints service secrets,
SOPS-encrypts the vault to your own age key), then chains the playbooks. For
an unattended run, pass `-i install.yaml --no-confirm`. Full reference
(prerequisites, secrets model, inventory layout, every subcommand):
[ansible/README.md](ansible/README.md).

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
