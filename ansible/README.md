# catena-ce ansible (Community base)

The deployment automation for a Catena Community host, plus the
installer that drives it. For the install walkthrough itself see
[../INSTALL.md](../INSTALL.md); this page describes what the pieces are.

## The five flows

```
preflight  ->  bootstrap  ->  site  ->  validate          (+ restore for DR)
```

- **preflight** -- controller-side check that the supplied Tailscale
  OAuth client is valid before any VPS work.
- **bootstrap** -- first-contact hardening of a fresh VPS (user, SSH,
  ufw, docker), then it joins the tailnet.
- **site** -- the converge: networking (Tailscale / Cloudflare Tunnel /
  coturn), Portainer, sign-on (Keycloak + oauth2-proxy), the restic
  backup, the catena-admin shell.
- **validate** -- on-host, tailnet and external checks.
- **restore** -- whole-host disaster recovery.

Each is one playbook and one atomic unit, with no cross-playbook
imports. Composition lives in the installer, which is why
`ansible-playbook` is not a supported entry point.

## Scheduled work is default-deny

Community ships ONE backup timer: a weekly backup
(`catena-backup.timer`, rate-limited to weekly-or-sparser by the
`backup_weekly_cap` filter -- a tighter `.env` value fails the
converge), plus a handful of local maintenance timers (disk watchdog,
monitor sync, antivirus watch, mail canary) and an on-demand version/CVE
check. Everything else is manual. A new `.timer` needs a deliberate
allowlist entry, machine-enforced rather than conventional.

The managed lifecycle -- daily and sub-daily backup cadence, secondary
and cold backup, auto-update with rollback, CVE remediation,
attestation, the catena-daily orchestrator chain -- is the Business
edition. Its engines ship in the public catena-admin payload and install
on every host, but the license check gates their features at runtime, so
they stay dormant on a Community host. On a Business host the managed
engine masks `catena-backup.timer` and takes over scheduling.

## Installer (`catena`)

The bundled CLI drives every flow. Prerequisite: `uv` on PATH
(ansible-core comes from `uv`). Nothing else -- there is no encryption
tool to install and no key to have in scope. Run it from this `ansible/`
directory, where `pyproject.toml` lives.

| Command | What it does |
| --- | --- |
| `install` | Seed the configuration, then run preflight, bootstrap, site, validate |
| `converge` | Re-run `site.yml` after a configuration or app change |
| `validate` | On-host + tailnet + external checks |
| `backup` | Take an on-demand snapshot |
| `restore` | In-place whole-host restore |
| `recover` | Rebuild onto a fresh replacement box |
| `uninstall` | Hand unattended-upgrades back to the OS |

Each takes `--inventory <name>`. With no subcommand the CLI opens an
interactive menu; `catena --install` is an alias for `catena install`.
The script form `uv run ./catena <cmd>` also works, and is how the
maintainers' rehearsal suite invokes it.

`install` first runs `seed.py` (collects configuration, writes the
non-secret inventory, stages the vendor credentials to a transient 0600
file), then chains the four flows. `-i install.yaml --no-confirm` makes
it unattended.

## Secrets

**No secret ever persists on the controller.** Not encrypted, not
plaintext: `catena install` writes only non-secret files into the
inventory.

- The install-critical vendor credentials (Cloudflare API token,
  Tailscale OAuth id and secret) are prompted, live-validated, written
  to a **transient 0600 file** that the CLI threads onto the converge as
  `-e @file`, and then deleted. The on-box loader adopts them into the
  store.
- Every other secret -- internal service secrets AND the user-held admin
  and restic passwords -- is minted **on the server**
  (`helpers/onbox_config.py`). The installer shows the admin and restic
  passwords **once** at the end of install
  (`playbooks/show_dr_keyset.yml`).
- The restic repo URL and S3 keys are set **post-install in
  catena-admin** (Settings > Backup); `run-backup.sh` reads them from the
  store at runtime.

The on-box config store (`/etc/catena/config.json`, 0600 root) is the
sole runtime source of truth, and `/etc` rides the restic backup, so a
rebuild needs only the `{restic repo, S3 credentials, restic password}`
keyset. Full classification: [SECRETS.md](SECRETS.md).

## Layout

| Directory | What is in it |
| --- | --- |
| [playbooks/](playbooks/) | The five flows plus the day-two operations, and the filter plugins Ansible loads from beside them |
| [roles/](roles/) | One role per thing a server owns |
| [helpers/](helpers/) | Python shared by the installer, the roles, and three host-side reconcilers |
| [scripts/](scripts/) | Executables installed on the server and run there |
| [inventory/](inventory/) | Per-deployment configuration; only `example/` is tracked |
| [tests/](tests/) | Unit tests, plus the external probes `validate.yml` runs |

Each has its own `README.md`. The `helpers/`, `scripts/`, `playbooks/`
and `roles/` indexes are generated from the headers of the files they
list, so a file that lands without a header shows up in its index as a
hole.
