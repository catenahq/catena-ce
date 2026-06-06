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
  coturn), Dokploy, basic SSO (Keycloak + oauth2-proxy), single Restic
  backup, the catena-admin shell.
- **validate** -- on-host + tailnet + external checks.
- **restore** -- whole-host disaster recovery.

Community is **manual**: there is no scheduled automation here. A user who
wants a backup schedule wires their own timer. The managed lifecycle
(secondary/cold backup, auto-update + rollback, CVE pipeline, attestation)
is the Business edition and ships separately as license-gated binaries; it
is never plaintext in this repo.

## Installer (`./catena`)

You drive everything through the bundled CLI; you never call
`ansible-playbook` directly. Prerequisites: `sops` and `age` on PATH
(ansible-core comes from `uv`).

```
uv run ./catena install --inventory prod     # seed + preflight/bootstrap/site/validate
uv run ./catena converge --inventory prod    # re-run site.yml after a config change
uv run ./catena validate --inventory prod    # on-host + tailnet + external checks
uv run ./catena restore  --inventory prod    # whole-host disaster recovery
uv run ./catena uninstall --inventory prod   # hand unattended-upgrades back to the OS
```

`install` first runs `seed.py` (collects config, mints service secrets,
SOPS-encrypts the vault), then chains the four playbooks. For an
unattended run, pass `-i install.yaml --no-confirm`.

`uninstall` does **not** delete your apps or data. It unmasks and
re-enables Debian's `apt-daily-upgrade.timer` so the box keeps patching
itself once Catena stops managing it, and prints the remaining teardown
steps (Dokploy, Cloudflare, Tailscale, your backup bucket) for you to do
deliberately.

## Secrets

Secrets are SOPS-encrypted with age (not ansible-vault). Each inventory
carries `group_vars/all/vault.sops.yml`, auto-decrypted at parse time by the
`community.sops` vars plugin. Community is self-hosted: the vault is
encrypted to **one** recipient -- your own age key. `seed.py` mints it on
first install, saves it to `~/.config/sops/age/keys.txt`, and shows it once
(back it up -- there is no operator with a copy). The installer loads it
into `$SOPS_AGE_KEY` automatically on later runs.

## Inventory

The installer writes `inventory/<name>/` for you (`.env`, the
SOPS-encrypted `vault.sops.yml`, `.sops.yaml`, `hosts.yml`). Real
inventories are gitignored; only `inventory/example/` is tracked as the
documented schema.

## Status

The Community base is complete: the controller skeleton, all shared roles,
the four converge playbooks (+ restore + uninstall), the manual backup
scripts, the catena-admin role, the inventory example, and the
`./catena` installer (with `seed.py`) all landed in the M1.2 decomposition.
