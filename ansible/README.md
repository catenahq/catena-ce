# catena-ce ansible (Community base)

The automation that installs and maintains a Catena Community host. It runs
on the server itself, from the copy inside a catena-admin release image: the
server installs itself from the release it runs, and converges itself with
the same tree. For the install walkthrough see [../README.md](../README.md);
the installer that starts it from a client's machine is in
[../installer/](../installer/). This page describes what the pieces are.

## The flows

```
install:  accounts  ->  bootstrap  ->  show-keyset  ->  converge  ->  validate
host:     reconcile (catena-converge), lockdown, restore
```

[install-host.sh](install-host.sh) runs the install on the server, as root,
with a one-host local inventory it writes for the run. The installer's
starter fetches the release image's tree and payload by digest
(`helpers/fetch_release.py`) and hands over to it, in two legs, each through
its own SSH session:

- **accounts** (`playbooks/accounts.yml`) -- through the provider's login:
  the `ops` and `panel` accounts with the installer's public key, and ops'
  sudo. Nothing is closed.
- **system** -- through a fresh login as `ops` with that key, which
  `helpers/session_proof.py` proves from sshd's own record before anything
  runs:
  - **bootstrap** -- the baseline and the sshd hardening, the on-box config
    store seeded from the installer's `.env`, and the ansible-core the host
    reconciles itself with;
  - **show-keyset** -- the passwords the server minted, printed between the
    KEYSET markers, and again when the install ends;
  - **converge** -- networking (Cloudflare Tunnel / coturn), Docker,
    Portainer, sign-on (Keycloak + oauth2-proxy), the restic backup, the
    catena-admin shell. Whatever the store does not configure yet (a domain,
    backups) is skipped and says so. `--tags` scopes it;
  - the version the host now runs, recorded for a later install and the
    panel's update (`catena-stack-update-managed pin`);
  - **validate** -- the on-host checks (vantages 1 and 2). The installer then
    checks the server from the client's machine.

The public port 22 stays open after the install.

- **reconcile** -- what the host's own converge runs, from the tree in the
  image the panel runs (catena-admin `catena-converge`).
- **lockdown** -- run by the panel's Lockdown on the host: joins the tailnet,
  proves the path, and closes public port 22; the port reconciler reopens it
  while the tailnet is down.
- **restore** -- whole-host disaster recovery, from the panel.

Each is one playbook and one atomic unit, with no cross-playbook imports.
Composition lives in install-host.sh and in catena-converge, which is why
`ansible-playbook` is not a supported entry point. The tree needs
ansible-core alone: no Galaxy collection.

The roles running a converge and the engines it installs come from one
release: `reconcile/roles/payload` refuses engines from an image built from
another catena-ce tree (`helpers/tree_hash.py`).

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

## The inventory

An inventory lives on the client's machine and holds non-secret files only:
its `.env` (the server's address, its SSH port and initial login, the key
file, the admin email, the site settings) and `hosts.yml` (the server's
name). `seed.py` is their one reader and writer, for the installer page and
the console alike, and `skel/` is the `hosts.yml` it scaffolds. The installer
sends the `.env` with each leg, and install-host.sh puts it beside the run's
inventory, where the `dotenv` lookup reads it (`playbooks/lookup_plugins/`).

## Secrets

**No secret ever persists on the client's machine.** Not encrypted, not
plaintext: the installer writes only non-secret files into the inventory.

- The install takes no vendor credential. The tailnet credential, the
  Cloudflare API token, the restic repo URL and the S3 keys are entered
  **post-install in catena-admin** > Settings, which writes them to the
  store.
- Every other secret is minted **on the server**
  (`helpers/onbox_config.py`). The installer shows the admin and console
  passwords **once**, right after bootstrap, and repeats them when the
  install ends (`playbooks/show-keyset.yml`); the restic password is generated in
  catena-admin > Settings > Backup and shown there once
  (`scripts/catena-restic-key.py`). An install's input file may pin the admin
  password; it reaches the server through a **transient 0600 file**
  (`-e @file`) that is deleted afterwards.

The on-box config store (`/etc/catena/config.json`, 0600 root) is the
sole runtime source of truth, and `/etc` rides the restic backup, so a
rebuild needs only the `{restic repo, S3 credentials, restic password}`
keyset. Full classification: [SECRETS.md](SECRETS.md).

## Layout

| Directory | What is in it |
| --- | --- |
| [playbooks/](playbooks/) | The flows plus the day-two operations, and the filter plugins Ansible loads from beside them |
| [bootstrap/roles/](bootstrap/roles/) | Roles only the install applies |
| [reconcile/roles/](reconcile/roles/) | Roles a server runs against itself |
| [helpers/](helpers/) | Python shared by the installer, the install on the server, the roles, and the host-side reconcilers |
| [scripts/](scripts/) | Executables installed on the server and run there |
| [inventory/](inventory/) | Inventories in a checkout, untracked |
| [tests/](tests/) | Unit tests |

Each has its own `README.md`. The `helpers/`, `scripts/`, `playbooks/`,
`bootstrap/roles/` and `reconcile/roles/` indexes are generated from the
headers of the files they list, so a file that lands without a header
shows up in its index as a hole.
