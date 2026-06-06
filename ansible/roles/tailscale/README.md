# tailscale

Install Tailscale from the upstream Debian repository (codename-aware:
the repo URL is templated against the host's `ansible_distribution_release`
so the same role works on bookworm + trixie + future codenames without
edits) and join the operator's tailnet.

## Auth flow

This role NEVER uses pre-auth keys. Every join mints a fresh,
ephemeral, single-use auth key via the OAuth client kept in the
inventory vault. Lifetime: 10 minutes; consumed by `tailscale up`
on the host. The OAuth client itself stays in vault; if it leaks,
rotation is the runbook (no auth keys to revoke retroactively).

The OAuth client must have `Auth Keys: Write` + `Devices Core: Write`
scopes (validated by `playbooks/preflight.yml`). If a join fails
because of a missing scope, preflight prints the admin-console URL
inline rather than carrying on with a broken client.

## Inputs

- `vault_tailscale_oauth_client_id` /
  `vault_tailscale_oauth_client_secret`
- `tailscale_tags` -- applied to the device at join time
  (`tag:catena-vps`, `tag:client-<id>`).
- `tailscale_advertise_tags` -- used for ACL routing.

## Side effects

- Adds the Tailscale apt source.
- Installs `tailscale`, enables the service.
- `tailscale up --auth-key=<minted>`.
- Records the joined device's tailnet IP into ansible_host's group
  facts so subsequent roles (storage, docker) see it.

## Idempotency

- `tailscale status` is checked before minting; an already-joined
  device with the right tags is a no-op (no key minted, no API
  call).

## Related

- Operator-facing: `runbooks/rotate-tailscale-oauth.md`.
- Preflight: `playbooks/preflight.yml`.
