# Catena Community

Self-host a complete business suite you own end to end. Catena installs a
curated catalog of open-source business apps onto your own VPS, wires them
behind single sign-on, and hands you monitoring, backups, and whole-host
restore -- all driven from one command. No telemetry, no license required,
no vendor lock-in. Source-available (fair-code).

Community is a complete, standalone product: the full app catalog plus the
whole base lifecycle -- one-command install, single sign-on across every app,
monitoring basics, on-demand backups, and whole-host restore/recovery onto a
fresh replacement box. Every operation runs through the `catena` CLI; every
byte of your data stays in standard formats you can read and restore with
standard tools, with or without Catena.

> A managed **Business** edition adds the catena-admin web panel plus
> operated extras on top (offsite immutable backups, automated updates, and
> more). The panel is a convenience layer over the same host-native
> automation in this repo -- removing it never takes your data or your
> ability to operate the suite. Learn more at
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

This is the public, fair-code base: the whole orchestration + installer.
The catena-admin web panel is NOT here: since the 2026-07 unification it
lives privately in `catenahq/catena-admin` and ships as a private container
image. The panel is a convenience layer over the host-native automation in
THIS repo -- everything it does (backups, restore, validate, converge) is
runnable here without it. See [LICENSE](LICENSE).

- **Base automation** -- the `preflight` / `bootstrap` / `site` / `validate` /
  `restore` flows + shared roles + the single-backup runner.
- **Installer / CLI** -- `ansible/catena`, a thin entry point so self-hosters
  never touch raw Ansible.
- **License wire format** -- the public `license` Go package: the ed25519
  token format Business hosts verify OFFLINE (the verify side is public on
  purpose, so the check is auditable).

```
ansible/               the Community deploy automation + the CLI
  catena               the installer / CLI entry point (see ansible/README.md)
  playbooks/ roles/    preflight/bootstrap/site/validate/restore + shared roles
  seed.py              config seeding (first-run secret minting happens on-box)
license/               ed25519 license-token wire format (offline verify + grace)
deploy/catena-admin/   the catena-admin container compose (public GHCR image)
```

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
(hand-written intent + machine-checked invariants) and
[VALIDATION.md](VALIDATION.md) (generated test-coverage sheet; drift fails the
maintainers' CI).

### Develop

Requires Go 1.26+ (for the license package) and uv (for the installer).

```
go build ./...
go vet ./...
go test ./...
```
