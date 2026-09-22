# tailscale

Install Tailscale from the upstream Debian repository (codename-aware:
the repo URL is templated against the host's `ansible_distribution_release`
so the same role works on bookworm + trixie + future codenames without
edits) and join the host to the tailnet its stored credentials belong to.

## Control server

`tailnet_provider` picks the fork, and both ends converge on `tailscale up`:

| Value | Control server | Credential minted by |
|---|---|---|
| `tailscale` | Tailscale SaaS | `POST /api/v2/tailnet/-/keys` with a bearer token exchanged from the OAuth client |
| `headscale` | self-hosted, at `tailnet_control_url` | `POST {control_url}/api/v1/preauthkey` with `headscale_api_key`, falling back to the static `headscale_preauth_key` |

The value is store-owned (catena-admin > Settings). A host whose store
predates the choice has nothing to read, so `defaults/main.yml` infers it:
a control-server URL means Headscale, no URL means Tailscale SaaS. That
inference is the fallback, and every task reads the declared value.

## Auth flow

This role never leaves a long-lived credential on the VPS. Every join mints
a fresh single-use key on the CONTROLLER, with the tags in `tailscale_tags`
and a TTL of `tailscale_auth_key_ttl_seconds` (10 minutes), consumed by one
`tailscale up` on the host. If the client itself leaks, rotation is the
runbook -- there are no auth keys to revoke retroactively.

The Headscale fork accepts a static `headscale_preauth_key` when no API key
is stored. That one IS long-lived, which is why the API key is preferred.

[../../../playbooks/preflight.yml](../../../playbooks/preflight.yml) proves
the Tailscale OAuth client before any VPS is touched: token exchange, then a
60-second throwaway mint carrying the first configured tag. Either failure
prints the admin-console clicks inline (ACL `tagOwners` entry, client scopes,
client tag authorization) rather than carrying on with a broken client.

## Inputs

All credentials come from the on-box store, seeded once from the inventory
`.env`.

- Tailscale SaaS: `tailscale_oauth_client_id`,
  `tailscale_oauth_client_secret`.
- Headscale: `tailnet_control_url`, `tailnet_headscale_user` (pre-auth keys
  are per-user), `headscale_api_key` or `headscale_preauth_key`.
- `tailscale_tags` -- both the minted key's tag list and `--advertise-tags`.
  Declared in
  [../../../playbooks/group_vars/all/main.yml](../../../playbooks/group_vars/all/main.yml),
  which reads `TAILSCALE_TAGS` with NO default: a missing key fails the
  lookup. The tag must be in the control server's `tagOwners` and the
  credential must be authorized for it.
- `tailscale_hostname` (the inventory hostname), `tailscale_accept_dns`,
  `tailscale_ephemeral`, `tailscale_force_reauth`.

## Side effects

- Adds the Tailscale apt keyring + source, installs `tailscale`, enables
  `tailscaled`.
- `tailscale up` with the minted key (plus `--login-server` on the Headscale
  fork).
- Exposes the joined node's tailnet IPv4 as the `tailscale_ipv4` fact.
  `bootstrap.yml` reads the same address straight afterwards and `add_host`es
  it as the steady-state host's `ansible_host`, so every downstream play in
  that invocation connects over the tailnet.
- Probes port 22 at that address from the controller (120s budget), so a node
  that is `Running` but unroutable fails here instead of as a misleading SSH
  error in the next role.

## Idempotency

`tailscale status --json` is read before anything is minted. A node already
in `Running` state is a no-op -- no key minted, no API call -- unless
`tailscale_force_reauth` is set.

## Related

- [../../../playbooks/rotate-tailscale.yml](../../../playbooks/rotate-tailscale.yml)
  -- explicit re-auth (runs this role with `tailscale_force_reauth: true`).
- [tasks/validate.yml](tasks/validate.yml) -- asserts package, keyring, apt
  source, `tailscaled` running + enabled, `tailscale0` present, and each
  configured tag in the live status JSON.
