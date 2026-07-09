# catena-ce ansible (Community base)

The fair-code deployment automation for a Catena Community host. A
self-hoster runs these playbooks (via the bundled installer/CLI, not raw
`ansible-playbook`) to bring up and maintain their own VPS:

```
preflight  ->  bootstrap  ->  site  ->  validate          (+ restore for DR)
```

- **preflight** -- controller-side check that the Tailscale OAuth client in
  the vault is valid before any VPS work.
- **bootstrap** -- first-contact hardening of a fresh VPS (user, SSH, ufw,
  docker), then it joins the tailnet.
- **site** -- the converge: networking (Tailscale / Cloudflare Tunnel /
  coturn), Portainer, basic SSO (Keycloak + oauth2-proxy), single Restic
  backup, the catena-admin shell.
- **validate** -- on-host + tailnet + external checks.
- **restore** -- whole-host disaster recovery.

Community ships exactly ONE timer: a single daily backup
(`catena-backup.timer`), plus an on-demand version/CVE check, so a
self-hosted CE deployment is credible without a license. Everything else
stays manual. The managed lifecycle (sub-daily backup cadence,
secondary/cold backup, auto-update + rollback, CVE remediation,
attestation, the catena-daily orchestrator chain) is the Business edition
and ships separately as license-gated binaries; it is never plaintext in
this repo. On a Business host the EE engine masks `catena-backup.timer`
and takes over scheduling.

## Installer (`./catena`)

You drive everything through the bundled CLI; you never call
`ansible-playbook` directly. Prerequisite: `uv` on PATH (ansible-core comes
from `uv`). SOPS+age was dropped (project 0b) -- there is no `sops`/`age`
prerequisite anymore.

```
uv run ./catena install --inventory prod     # seed + preflight/bootstrap/site/validate
uv run ./catena converge --inventory prod    # re-run site.yml after a config change
uv run ./catena validate --inventory prod    # on-host + tailnet + external checks
uv run ./catena restore  --inventory prod    # whole-host disaster recovery
uv run ./catena uninstall --inventory prod   # hand unattended-upgrades back to the OS
```

`install` first runs `seed.py` (collects config, mints service secrets,
writes the plaintext 0600 vault), then chains the four playbooks. For an
unattended run, pass `-i install.yaml --no-confirm`.

`uninstall` does **not** delete your apps or data. It unmasks and
re-enables Debian's `apt-daily-upgrade.timer` so the box keeps patching
itself once Catena stops managing it, and prints the remaining teardown
steps (Portainer, Cloudflare, Tailscale, your backup bucket) for you to do
deliberately.

## Secrets

SOPS+age was dropped (project 0b). Each inventory carries a **plaintext,
gitignored, 0600** `group_vars/all/vault.yml`, loaded at parse time by the
stock `host_group_vars` vars plugin -- no `community.sops` plugin, no age
key, no `.sops.yaml` recipient policy, no `$SOPS_AGE_KEY`. `seed.py` mints
the auto-generated values on first install and writes them straight into
that file.

At converge time the on-box config store (`/etc/catena/config.json`, 0600
root) becomes the runtime source of truth: the loader adopts the vault
values, mints any missing internal secret, and every downstream role reads
the store. `/etc` rides the restic backup, so a rebuild needs only the
`{restic endpoint, S3 creds, restic password}` keyset -- the plaintext vault
is just the install-time seed.

## Inventory

The installer writes `inventory/<name>/` for you (`.env`, the plaintext
`group_vars/all/vault.yml`, `hosts.yml`). Real inventories are gitignored;
only `inventory/example/` is tracked as the documented schema.

## Status

The Community base is complete: the controller skeleton, all shared roles,
the four converge playbooks (+ restore + uninstall), the manual backup
scripts, the catena-admin role, the inventory example, and the
`./catena` installer (with `seed.py`) all landed in the M1.2 decomposition.
