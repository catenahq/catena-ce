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

## Secrets

Secrets are SOPS-encrypted with age (not ansible-vault). Each inventory
carries `group_vars/all/vault.sops.yml`, auto-decrypted at parse time by the
`community.sops` vars plugin. The operator age key is supplied via
`$SOPS_AGE_KEY` in the calling shell, never written to disk.

## Inventory

Copy `inventory/example` to `inventory/<name>`, fill in `.env` + the
SOPS-encrypted `vault.sops.yml`, and target it with `-i inventory/<name>`.
Real inventories are gitignored; only the example is tracked.

## Status

This base is being assembled by decomposing the operator monorepo into the
CE/EE split. Landed so far: the controller skeleton (config, collections,
filter/lookup plugins, preflight). Roles, the converge playbooks, the manual
scripts, the catena-admin role, the inventory example, and the installer
land in subsequent slices.
