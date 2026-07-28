# catena-ce ansible (Community base)

The fair-code deployment automation for a Catena Community host. A
self-hoster runs these playbooks (via the bundled installer/CLI, not raw
`ansible-playbook`) to bring up and maintain their own VPS:

```
preflight  ->  bootstrap  ->  site  ->  validate          (+ restore for DR)
```

- **preflight** -- controller-side check that the supplied Tailscale OAuth
  client is valid before any VPS work.
- **bootstrap** -- first-contact hardening of a fresh VPS (user, SSH, ufw,
  docker), then it joins the tailnet.
- **site** -- the converge: networking (Tailscale / Cloudflare Tunnel /
  coturn), Portainer, basic SSO (Keycloak + oauth2-proxy), single Restic
  backup, the catena-admin shell.
- **validate** -- on-host + tailnet + external checks.
- **restore** -- whole-host disaster recovery.

Community ships ONE backup timer: a weekly backup
(`catena-backup.timer`, rate-limited to weekly-or-sparser by the
`backup_weekly_cap` filter -- a tighter .env value fails the converge),
plus a handful of local maintenance timers (disk watchdog, monitor
sync, antivirus watch, mail canary) and an on-demand version/CVE check,
so a self-hosted CE deployment has a proven scheduled-backup lane
without a license. Everything else stays manual. The managed lifecycle
(daily and sub-daily backup cadence, secondary/cold backup, auto-update
+ rollback, CVE remediation, attestation, the catena-daily orchestrator
chain) is the Business edition: its engines ship in the public
catena-admin payload and install on every host (compiled Go binaries
whose source lives in the private repo, not in this tree), but the
license check gates their
features at runtime -- they stay dormant on a Community host. On a
Business host the EE engine masks `catena-backup.timer` and takes over
scheduling.

## Installer (`catena`)

The bundled CLI drives every playbook; `ansible-playbook` is not a
supported entry point. Prerequisite: `uv` on PATH (ansible-core comes from
`uv`). Nothing else -- there is no encryption tool to install and no key to
have in scope. Run the CLI **from this `ansible/` directory** (where
`pyproject.toml` lives):

```
uv run catena install --inventory prod     # seed + preflight/bootstrap/site/validate
uv run catena converge --inventory prod    # re-run site.yml after a config change
uv run catena validate --inventory prod    # on-host + tailnet + external checks
uv run catena restore  --inventory prod    # whole-host disaster recovery
uv run catena uninstall --inventory prod   # hand unattended-upgrades back to the OS
```

Run `uv run catena` with no subcommand for an interactive menu; `catena
--install` is an alias for `catena install`. The equivalent script form
`uv run ./catena <cmd>` still works (the bench and the Semaphore wrapper
invoke the `catena` file directly).

`install` first runs `seed.py` (collects config, writes the non-secret
inventory, stages the vendor creds to a transient 0600 file), then chains
the four playbooks. For an unattended run, pass `-i install.yaml
--no-confirm`.

`uninstall` does **not** delete your apps or data. It unmasks and
re-enables Debian's `apt-daily-upgrade.timer` so the box keeps patching
itself once Catena stops managing it, and prints the remaining teardown
steps (Portainer, Cloudflare, Tailscale, your backup bucket) for you to do
deliberately.

## Secrets

**No secret ever persists on the controller** (0b). There is no encrypted
vault and no plaintext one either: `catena install` writes only non-secret
files into the inventory.

- The install-critical vendor creds (Cloudflare API token + Tailscale OAuth
  id/secret) are prompted, live-validated, written to a **transient 0600 file**
  that the CLI threads onto the converge as `-e @file`, and then deleted. The
  on-box loader adopts them into the store.
- Every other secret -- internal service secrets AND the user-held admin +
  restic passwords -- is minted **on-box** (`helpers/onbox_config.py`). The
  installer shows the admin + restic passwords **once** at the end of install
  (`playbooks/show_dr_keyset.yml`); save them in your password manager.
- The restic repo URL + S3 keys are set **post-install in catena-admin**
  (Settings > Backup); `run-backup.sh` reads them from the store at runtime.

The on-box config store (`/etc/catena/config.json`, 0600 root) is the sole
runtime source of truth; `/etc` rides the restic backup, so a rebuild needs
only the `{restic repo, S3 creds, restic password}` keyset. Full
classification: [SECRETS.md](SECRETS.md).

## Inventory

The installer writes `inventory/<name>/` for you (`.env` + `hosts.yml` --
non-secret only; no vault). Real inventories are gitignored; only
`inventory/example/` is tracked as the documented schema.

## Status

The Community base is complete: the controller skeleton, all shared roles,
the four converge playbooks (+ restore + uninstall), the manual backup
scripts, the catena-admin role, the inventory example, and the
`./catena` installer (with `seed.py`) all landed in the M1.2 decomposition.
