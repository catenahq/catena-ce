# catena-ce ansible (Community base)

The deployment automation for a Catena Community host, plus the
installer that drives it. For the install walkthrough itself see
[../README.md](../README.md); this page describes what the pieces are.

## The flows

```
install:  bootstrap  ->  converge  ->  validate
panel:    lockdown, restore
```

The install reaches the host over its public SSH address and configures
nothing beyond reaching and installing it; public port 22 stays open after
the install. The panel and Portainer answer the host's loopback only and are
reached through an SSH forward as the `panel` account, which can do nothing but
forward. The domain, the tailnet and the backups are entered in the panel.

- **bootstrap** -- first-contact hardening of a fresh VPS (user, SSH,
  ufw) and the on-box config store.
- **converge** -- the converge: networking (Cloudflare Tunnel / coturn),
  Docker, Portainer, sign-on (Keycloak + oauth2-proxy), the restic backup,
  the catena-admin shell. Whatever the store does not configure yet (a
  domain, backups) is skipped and says so.
- **validate** -- on-host, tailnet and external checks.
- **lockdown** -- run by the panel's Lockdown on the host: joins the tailnet,
  proves the path, and closes public port 22; the port reconciler reopens it
  while the tailnet is down.
- **restore** -- whole-host disaster recovery, from the panel.

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
they stay dormant on a Community host. On a Business host `catena-schedule`
turns on the lanes set on the panel's Schedules page.

## Installer (`catena-cli`)

The bundled CLI reaches the server over SSH. Prerequisite: `uv` on PATH
(ansible-core comes from `uv`). Nothing else -- there is no encryption
tool to install and no key to have in scope. Run it from the repository
root, whose project depends on this one, or from this `ansible/`
directory, where its own `pyproject.toml` lives. The graphical installer
(`uv run catena-gui`, in `../gui/`) writes the same inventory and runs
the same `install`.

| Command | Playbook | What it does |
| --- | --- | --- |
| `init` | none | Create `inventory/<name>/.env`, every key at its default and explained, to fill in |
| `install` | chain | Seed the configuration, run bootstrap, show the passwords (`show-keyset.yml`), run converge and validate, then show the passwords again |
| `converge` | `converge.yml` | Re-apply from this machine; `--address` reaches the host at its tailnet address once the panel's Lockdown has closed public SSH |
| `uninstall` | `uninstall.yml` | Unmask the native apt timers on a host an older release masked |

One shape: `uv run catena-cli <verb> --inventory <name>`. A verb that runs a
single playbook carries that playbook's name; `install` chains several, so
there is no one playbook to name it after. Backups, the tunnel, the tailnet,
the lockdown and restores run from the panel, on the installed host. With no
arguments the CLI opens an interactive menu and prompts for both; `catena-cli
--install` is an alias for `catena-cli install`. A leading inventory name is
refused with the correct shape rather than an argparse choice error.

`install` first runs `seed.py`: with no `-i`, `.env` must already exist
(written by `init` or the graphical installer, and filled in), and seed
reads its config from there instead of prompting field by field. `hosts.yml`
auto-scaffolds from `skel/` on that same first run; nothing else to copy or
edit. `-i install.yaml --no-confirm` generates a fresh
inventory from an answers file instead (the bench / power-user path),
unattended; an answers file naming a value the server holds (the domain, the
tailnet, backups, a vendor credential) is refused. The CLI then asks for the
provider's password only when the SSH key does not open the server already.

## Secrets

**No secret ever persists on the controller.** Not encrypted, not
plaintext: `catena-cli install` writes only non-secret files into the
inventory.

- The install takes no vendor credential. The tailnet credential, the
  Cloudflare API token, the restic repo URL and the S3 keys are entered
  **post-install in catena-admin** > Settings, which writes them to the
  store.
- Every other secret is minted **on the server**
  (`helpers/onbox_config.py`). The installer shows the admin and console
  passwords **once**, right after bootstrap, and repeats them when the
  install ends (`playbooks/show-keyset.yml`); the restic password is generated in
  catena-admin > Settings > Backup and shown there once
  (`scripts/catena-restic-key.py`). An install.yaml may pin the admin
  password; it reaches the converge through a **transient 0600 file**
  (`-e @file`) that is deleted afterwards.

The on-box config store (`/etc/catena/config.json`, 0600 root) is the
sole runtime source of truth, and `/etc` rides the restic backup, so a
rebuild needs only the `{restic repo, S3 credentials, restic password}`
keyset. Full classification: [SECRETS.md](SECRETS.md).

## Layout

| Directory | What is in it |
| --- | --- |
| [playbooks/](playbooks/) | The flows plus the day-two operations, and the filter plugins Ansible loads from beside them |
| [bootstrap/roles/](bootstrap/roles/) | Operator-run roles, from outside the server |
| [reconcile/roles/](reconcile/roles/) | Roles a server runs against itself |
| [helpers/](helpers/) | Python shared by the installer, the roles, and three host-side reconcilers |
| [scripts/](scripts/) | Executables installed on the server and run there |
| [inventory/](inventory/) | Per-deployment configuration, untracked |
| [tests/](tests/) | Unit tests, plus the external probes `validate.yml` runs |

Each has its own `README.md`. The `helpers/`, `scripts/`, `playbooks/`,
`bootstrap/roles/` and `reconcile/roles/` indexes are generated from the
headers of the files they list, so a file that lands without a header
shows up in its index as a hole.
