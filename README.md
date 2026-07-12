# catena-ce

The public, fair-code base of **Catena** -- a self-hostable business suite you
own and run yourself. Community is a complete, standalone product: the full
app catalog plus the whole base lifecycle -- one-command install, single
sign-on across every app, monitoring basics, on-demand backups, and
whole-host restore/recovery onto a fresh replacement box. Source-available,
no telemetry, no license required.

> A managed **Business** edition adds operated extras on top (offsite
> immutable backups, automated updates, and more). Learn more at
> [catena.run](https://catena.run).

This repository holds:

- **catena-admin (Go shell)** -- the Community admin surface: a single
  binary hosting the Community panels + actions, with a plugin seam that a
  Business license extends at runtime (no second build).
- **Base automation** -- the `preflight` / `bootstrap` / `site` /
  `validate` / `restore` flows + shared roles + the single-backup runner.
- **Installer / CLI** -- `ansible/catena`, a thin entry point so
  self-hosters never touch raw Ansible (see [Install](#install-self-host)).

Enterprise (Business) code is NOT here: it lives privately in
`catenahq/catena-ee` and ships as compiled, license-gated
binaries. See [LICENSE](LICENSE).

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
(hand-written intent + machine-checked invariants) and
[VALIDATION.md](VALIDATION.md) (generated test-coverage sheet; drift
fails the maintainers' CI).

## Layout

```
ansible/               the Community deploy automation + the CLI
  catena               the installer / CLI entry point (see ansible/README.md)
  playbooks/ roles/    preflight/bootstrap/site/validate/restore + shared roles
  seed.py              config + plaintext-vault seeding (first-run secret minting)
cmd/catena-admin/      the Go shell entry point
license/               ed25519 license-token wire format (offline verify + grace)
plugin/                the CE/EE plugin SDK contract (implemented by catena-ee)
internal/registry/     host-side store that gates Business plugins on a license
loader/                go-plugin loader for the downloaded EE plugin binaries
deploy/catena-admin/   the catena-admin container compose
```

## Install (self-host)

Self-hosters drive everything through the bundled **`catena` CLI** -- you
never call `ansible-playbook` directly. Prerequisite: `uv` on PATH
(ansible-core comes from `uv`). From the `ansible/` directory:

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
writes them to a plaintext 0600 vault), then chains the playbooks. For
an unattended run, pass `-i install.yaml --no-confirm`. Full reference
(prerequisites, secrets model, inventory layout, every subcommand):
[ansible/README.md](ansible/README.md).

## Secrets you hold

Catena generates and keeps every internal secret on the box (database
passwords, OIDC client secrets, service tokens, ...) -- they live on the
host and ride every backup, so you never have to hold them. You are
responsible for only two short lists. The full classification is in
[ansible/SECRETS.md](ansible/SECRETS.md).

**Before install** -- have these vendor credentials ready to enter (you
create them in each provider's console; catena cannot generate them):

- Cloudflare API token -- tunnel + DNS (`Account > Cloudflare Tunnel > Edit`,
  `Zone > DNS > Edit`).
- Tailscale OAuth client id + secret -- to join the tailnet (scope
  `Auth Keys: Write`, tag `tag:vps`).
- S3 access key + secret key -- for the object-storage bucket that holds your
  restic backup repository.
- (optional) SMTP or mail-relay password -- only if you enable outbound mail.

**After install** -- save these in your own password manager. Catena shows
each once and does not keep a copy you can recover from elsewhere:

- **Admin password** -- logs you into Portainer and Keycloak. Minted at
  install, shown once.
- **Restic encryption password** -- decrypts your backup repository. Minted at
  install, shown once. Without it your backups cannot be restored, by anyone.
- **Restic repo URL + S3 keys** -- together with the restic password, these
  three are the *entire* disaster-recovery keyset: with them alone you can
  rebuild a wiped VPS from backup onto a fresh box.

## Develop

Requires Go 1.26+.

```
go build ./...
go vet ./...
go test ./...
```

### Run the shell locally (dev only)

This is a **developer convenience**, not an install step. It runs the
catena-admin binary directly on your machine so you can iterate on the panel
-- it starts the admin GUI (dashboard + CE actions + license-gated EE plugin
panels) plus the `/healthz` and `/licensez` endpoints on a port. In a real
deployment you never run this: `catena install` deploys the **same** binary as
the catena-admin container (a Portainer stack on `:8000`), reached at
`https://dash.<zone>` behind oauth2-proxy SSO -- see [Install](#install-self-host).

```
# Community-only (no license): serves the GUI + /healthz + /licensez on :8080
go run ./cmd/catena-admin

# With a Business license (token + operator public key), to exercise the
# license-gated EE plugin panels:
CATENA_LICENSE="<token>" CATENA_LICENSE_PUBKEY="<base64-ed25519>" \
  go run ./cmd/catena-admin
```

`CATENA_ADMIN_ADDR` overrides the listen address (the container sets `:8000`).
With no (or an invalid) license the shell runs Community-only; it never fails
closed on a missing key.
