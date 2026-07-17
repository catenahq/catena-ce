# Catena Community

Self-host a complete business suite you own end to end. Catena installs a
curated catalog of open-source business apps onto your own VPS, wires them
behind single sign-on, and hands you monitoring, backups, and whole-host
restore -- all driven from one command. No telemetry, no license required,
no vendor lock-in. Source-available (fair-code).

Community is a complete, standalone product: the full app catalog plus the
whole base lifecycle -- one-command install, single sign-on across every app,
monitoring basics, on-demand backups, and whole-host restore/recovery onto a
fresh replacement box.

> A managed **Business** edition adds operated extras on top (offsite
> immutable backups, automated updates, and more). Learn more at
> [catena.run](https://catena.run).

## Quick start

You need a fresh Debian VPS and [`uv`](https://docs.astral.sh/uv/) on your
own machine -- that is the only prerequisite (ansible-core is pulled in by
`uv`). Clone this repo, then:

```
cd ansible
uv run catena install
```

`install` walks you through configuration, mints and stores every internal
secret on the box itself, then brings the host up:
`preflight -> bootstrap -> site -> validate`. Run `uv run catena` with **no
arguments** for an interactive menu of every operation.

Day-two operations run through the same CLI:

```
uv run catena converge   # re-apply after a config or app change
uv run catena validate   # on-host + tailnet + external health checks
uv run catena backup     # take an on-demand snapshot
uv run catena restore    # in-place whole-host restore
uv run catena recover    # rebuild onto a FRESH replacement box
```

Full reference -- every subcommand, the secrets model, inventory layout:
[ansible/README.md](ansible/README.md).

## What you need before install

Vendor credentials you create in each provider's console (Catena cannot
generate these -- have them ready to paste in):

- **Cloudflare API token** -- tunnel + DNS (`Account > Cloudflare Tunnel >
  Edit`, `Zone > DNS > Edit`).
- **Tailscale OAuth client id + secret** -- to join the private network
  (scope `Auth Keys: Write`, tag `tag:vps`).
- **S3 access key + secret key** -- the object-storage bucket that holds your
  restic backup repository.
- (optional) **SMTP or mail-relay password** -- only if you enable outbound
  mail.

## Secrets you hold

Catena generates and keeps every internal secret on the box (database
passwords, OIDC client secrets, service tokens) -- they live on the host and
ride every backup, so you never have to hold them. You are responsible for
only what Catena shows you once at install and cannot recover for you later:

- **Admin password** -- logs you into Portainer and Keycloak.
- **Restic encryption password + repo URL + S3 keys** -- together these three
  are the *entire* disaster-recovery keyset: with them alone you can rebuild a
  wiped VPS from backup onto a fresh box. Without the restic password your
  backups cannot be restored, by anyone. Save them in your own password
  manager.

Full classification: [ansible/SECRETS.md](ansible/SECRETS.md).

---

## What's in this repo (for contributors)

This is the public, fair-code base. Enterprise (Business) code is NOT here: it
lives privately in `catenahq/catena-ee` and ships as compiled, license-gated
binaries. See [LICENSE](LICENSE).

- **catena-admin (Go shell)** -- the Community admin surface: a single binary
  hosting the Community panels + actions, with a plugin seam that a Business
  license extends at runtime (no second build).
- **Base automation** -- the `preflight` / `bootstrap` / `site` / `validate` /
  `restore` flows + shared roles + the single-backup runner.
- **Installer / CLI** -- `ansible/catena`, a thin entry point so self-hosters
  never touch raw Ansible.

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

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
(hand-written intent + machine-checked invariants) and
[VALIDATION.md](VALIDATION.md) (generated test-coverage sheet; drift fails the
maintainers' CI).

### Develop

Requires Go 1.26+.

```
go build ./...
go vet ./...
go test ./...
```

#### Run the shell locally (dev only)

This is a **developer convenience**, not an install step. It runs the
catena-admin binary directly on your machine so you can iterate on the panel
-- it starts the admin GUI (dashboard + CE actions + license-gated EE plugin
panels) plus the `/healthz` and `/licensez` endpoints on a port. In a real
deployment you never run this: `catena install` deploys the **same** binary as
the catena-admin container (a Portainer stack on `:8000`), reached at
`https://dash.<zone>` behind oauth2-proxy SSO.

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
