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

The value is store-owned (catena-admin > Settings): `none`, `tailscale` or
`headscale`. `playbooks/lockdown.yml` runs the role only on a host whose value
is not `none`, and every task reads the stored value.

## Auth flow

This role never leaves a long-lived auth key on the VPS. Every join mints a
fresh single-use key where the play is driven from -- the host itself when
the panel's lockdown runs it, the controller when `playbooks/rotate-tailscale.yml`
runs from one -- with the
tags in `tailscale_tags` and a TTL of `tailscale_auth_key_ttl_seconds` (10
minutes), consumed by one `tailscale up` on the host. If the OAuth client
itself leaks, rotation is the runbook: there are no auth keys to revoke.

The Headscale fork accepts a static `headscale_preauth_key` when no API key
is stored. That one IS long-lived, which is why the API key is preferred.

[../../../playbooks/preflight.yml](../../../playbooks/preflight.yml) proves
a Tailscale OAuth client in scope before any VPS is touched: token exchange, a
60-second throwaway mint carrying the first configured tag, and a read of the
device list (the `Devices -> Core -> Read` scope `tasks/reachable.yml`
needs). Any failure prints the admin-console clicks inline (ACL `tagOwners`
entry, client scopes, client tag authorization) rather than carrying on with a
broken client.

## Reachability before the lockdown

[tasks/reachable.yml](tasks/reachable.yml), which `playbooks/lockdown.yml`
runs right after the join and before public 22 can close (only the panel's
Lockdown closes it), asks the tailnet rather than the host whether the host
is reachable
([../../../helpers/tailnet_reachability.py](../../../helpers/tailnet_reachability.py),
run on the host):

- the control server's record of the node: Tailscale's device API
  (`connectedToControl`, `blocksIncomingConnections`), or Headscale's node API
  (`online`, which needs `headscale_api_key`);
- on the host itself (`tailscale_on_host`, the panel's lockdown), an online
  peer carrying none of `tailscale_tags` answering a TSMP ping through the
  tunnel. From a controller, its TCP probe is that proof.

A refusal ends the lockdown with public 22 still open and the reason in its
log.

## Inputs

All credentials come from the on-box store, entered in catena-admin >
Settings.

- Tailscale SaaS: `tailscale_oauth_client_id`,
  `tailscale_oauth_client_secret`.
- Headscale: `tailnet_control_url`, `tailnet_headscale_user` (pre-auth keys
  are per-user), `headscale_api_key` or `headscale_preauth_key`.
- `tailscale_tags` -- both the minted key's tag list and `--advertise-tags`.
  Declared in
  [../../../playbooks/group_vars/all/main.yml](../../../playbooks/group_vars/all/main.yml),
  which reads the store's `TAILSCALE_TAGS` with NO default: a missing value
  fails the join. The tag must be in the control server's `tagOwners` and the
  credential must be authorized for it.
- `tailscale_hostname` (the inventory hostname), `tailscale_accept_dns`
  (false: the host keeps its own resolver), `tailscale_ephemeral`, `tailscale_force_reauth`, `tailscale_on_host` (true
  under catena-converge's local-connection inventory).

## Side effects

- Adds the Tailscale apt keyring + source, installs `tailscale`, enables
  `tailscaled`.
- `tailscale up` with the minted key (plus `--login-server` on the Headscale
  fork).
- Exposes the joined node's tailnet IPv4 as the `tailscale_ipv4` fact.
  `playbooks/lockdown.yml` runs this role first; from the panel's Lockdown it
  then closes public 22 behind it.
- From the controller, probes port 22 at that address (120s budget), so a
  node that is `Running` but unroutable fails here instead of as a misleading
  error in the lockdown. On the host itself that probe would dial its own
  address, so it is skipped there and `tasks/reachable.yml` answers instead.

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
