# catena-ce ansible (Community base)

The deployment automation for a Catena Community host, plus the
installer that drives it. For the install walkthrough itself see
[../README.md](../README.md); this page describes what the pieces are.

## The five flows

```
preflight  ->  bootstrap  ->  converge  ->  validate       (+ restore for DR)
```

- **preflight** -- controller-side check that the supplied Tailscale
  OAuth client is valid before any VPS work.
- **bootstrap** -- first-contact hardening of a fresh VPS (user, SSH,
  ufw, docker), then it joins the tailnet.
- **converge** -- the converge: networking (Tailscale / Cloudflare Tunnel /
  coturn), Portainer, sign-on (Keycloak + oauth2-proxy), the restic
  backup, the catena-admin shell.
- **validate** -- on-host, tailnet and external checks.
- **restore** -- whole-host disaster recovery.

Each is one playbook and one atomic unit, with no cross-playbook
imports. Composition lives in the installer, which is why
`ansible-playbook` is not a supported entry point.

## Scheduled work is default-deny

Every scheduled lane ships DISABLED, and enabling one needs an active
Catena Pro licence: `catena-schedule` verifies the licence on the host and
leaves an unlicensed box with every timer off. The mechanisms themselves are
Community and stay runnable from the panel's buttons -- what is licensed is
having them happen while nobody is watching.

Local maintenance timers are not lanes and are always on: monitor sync,
antivirus watch, mail canary, public-port reconciliation and the
reboot-required probe. Each is an enumerated allowlist entry, machine-enforced
rather than conventional, and none of them spends object storage.

The managed lifecycle -- scheduled backup, verification and offsite
copies, container updates with rollback, CVE remediation, attestation,
the catena-daily orchestrator chain -- is the Business edition. Its engines ship in the public catena-admin payload and install
on every host, but the license check gates their features at runtime, so
they stay dormant on a Community host. On a Business host the managed
engine masks `catena-backup.timer` and takes over scheduling.

## Installer (`catena`)

The bundled CLI drives every flow. Prerequisite: `uv` on PATH
(ansible-core comes from `uv`). Nothing else -- there is no encryption
tool to install and no key to have in scope. Run it from this `ansible/`
directory, where `pyproject.toml` lives.

| Command | Playbook | What it does |
| --- | --- | --- |
| `install` | chain | Seed the configuration, then run preflight, bootstrap, converge, validate |
| `recover` | chain | Rebuild onto a fresh replacement box |
| `rollback` | chain | Roll a still-running host back to a prior snapshot |
| `converge` | `converge.yml` | Re-apply after a configuration or app change |
| `validate` | `validate.yml` | On-host + tailnet + external checks |
| `backup` | `backup.yml` | Take an on-demand snapshot |
| `restore` | `restore.yml` | In-place whole-host restore |
| `rotate-tunnel` | `rotate-tunnel.yml` | Mint a new Cloudflare tunnel |
| `rotate-tailscale` | `rotate-tailscale.yml` | Force re-authentication to the tailnet |
| `show-keyset` | `show-keyset.yml` | Show the passwords and first-login URLs again |
| `uninstall` | `uninstall.yml` | Unmask the native apt timers on a host an older release masked |

One shape: `uv run catena <verb> --inventory <name>`. A verb that runs a
single playbook carries that playbook's name; the three that chain several
do not, because there is no one playbook to name them after. With no
arguments the CLI opens an interactive menu and prompts for both; `catena
--install` is an alias for `catena install`. A leading inventory name is
refused with the correct shape rather than an argparse choice error.

`install` first runs `seed.py`: with no `-i`, `.env` must already exist
(copied from `inventory/example/.env.example` and filled in -- the only
file in that directory, and the only one a self-hoster ever copies), and
seed reads its config from there instead of prompting field by field --
what it still prompts for is the tailnet join credential, and the
Cloudflare token when the inventory names a domain, both staged to a
transient 0600 file. `hosts.yml`/`localhost.yml` auto-scaffold
from `skel/` on that same first run; nothing else to copy or edit.
`-i install.yaml --no-confirm` generates a fresh inventory from an
answers file instead (the bench / power-user path), unattended.

## Secrets

**No secret ever persists on the controller.** Not encrypted, not
plaintext: `catena install` writes only non-secret files into the
inventory.

- The one install-critical vendor credential (Tailscale OAuth id and
  secret) is prompted, live-validated, written to a **transient 0600
  file** that the CLI threads onto the converge as `-e @file`, and then
  deleted. The on-box loader adopts it into the store. The Cloudflare API
  token is never an install input at all -- entered later in catena-admin
  > Settings.
- Every other secret -- internal service secrets AND the user-held admin
  and restic passwords -- is minted **on the server**
  (`helpers/onbox_config.py`). The installer shows the admin and restic
  passwords **once** at the end of install
  (`playbooks/show-keyset.yml`).
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
| [bootstrap/roles/](bootstrap/roles/) | Operator-run roles, from outside the server |
| [reconcile/roles/](reconcile/roles/) | Roles a server runs against itself |
| [helpers/](helpers/) | Python shared by the installer, the roles, and three host-side reconcilers |
| [scripts/](scripts/) | Executables installed on the server and run there |
| [inventory/](inventory/) | Per-deployment configuration; only `example/` is tracked |
| [tests/](tests/) | Unit tests, plus the external probes `validate.yml` runs |

Each has its own `README.md`. The `helpers/`, `scripts/`, `playbooks/`,
`bootstrap/roles/` and `reconcile/roles/` indexes are generated from the
headers of the files they list, so a file that lands without a header
shows up in its index as a hole.
