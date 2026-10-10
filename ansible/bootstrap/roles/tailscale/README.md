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
| `headscale` | self-hosted, at `tailnet_control_url` | `POST {control_url}/api/v1/preauthkey` with `headscale_api_key`, for the numeric id `GET {control_url}/api/v1/user` lists for `tailnet_headscale_user`, falling back to the static `headscale_preauth_key` |

The value is store-owned (catena-admin > Settings): `none`, `tailscale` or
`headscale`. `playbooks/lockdown.yml` runs the role only on a host whose value
is not `none`, and every task reads the stored value.

## Auth flow

This role never leaves a long-lived auth key on the VPS. Every join mints a
fresh single-use key on the host itself, where the panel's join, Lockdown and
re-join (`playbooks/rotate-tailscale.yml`) run the role, with the
tags in `tailscale_tags` and a TTL of `tailscale_auth_key_ttl_seconds` (10
minutes), consumed by one `tailscale up` on the host. If the OAuth client
itself leaks, rotation is the runbook: there are no auth keys to revoke.

The Headscale fork accepts a static `headscale_preauth_key` when no API key
is stored. That one IS long-lived, which is why the API key is preferred.

On Headscale the node takes its tags from the pre-auth key, and its
`tailscale up` advertises none: Headscale refuses a node that joins with a
pre-auth key and advertises tags. A static key therefore has to be created with
the tags in `tailscale_tags`. The oldest Headscale the fork supports is the
`HEADSCALE_MIN_VERSION` that
[../../../scripts/catena-tailnet-check.py](../../../scripts/catena-tailnet-check.py)
declares, which refuses an older server when the settings are saved.

## Reachability before the lockdown

[tasks/reachable.yml](tasks/reachable.yml), which `playbooks/lockdown.yml`
runs right after the join when the panel's Lockdown closes public 22, asks the
tailnet rather than the host whether the host is reachable
([../../../helpers/tailnet_reachability.py](../../../helpers/tailnet_reachability.py),
run on the host):

- the control server's record of the node: Tailscale's device API
  (`connectedToControl`, `blocksIncomingConnections`, which needs the
  `Devices -> Core -> Read` scope), or Headscale's node API (`online`, which
  needs `headscale_api_key`);
- an online peer carrying none of `tailscale_tags` answering a TSMP ping
  through the tunnel;
- the tailnet policy letting one of those peers open TCP 22 to the host: the
  packet filter its control server sent it, read from `tailscale debug
  netmap`. tailscaled answers the ping before that filter runs, and the
  control server reports the node online whatever its policy;
- the host's own firewall letting SSH in on `tailscale0`: a new TCP
  connection to port 22, addressed to the host's tailnet address, walked
  through its iptables and ip6tables rulesets in kernel order. The ping above
  is answered inside tailscaled and never crosses that interface, so only
  this check sees a rule that drops tailnet traffic there.

A refusal ends the lockdown with public 22 still open and the reason in its
log.

## Inputs

All credentials come from the on-box store, entered in catena-admin >
Settings.

- Tailscale SaaS: `tailscale_oauth_client_id`,
  `tailscale_oauth_client_secret`.
- Headscale: `tailnet_control_url`, `tailnet_headscale_user` (the user a
  minted key is created for), `headscale_api_key` or `headscale_preauth_key`.
- `tailscale_tags` -- the minted key's tag list, and `--advertise-tags` on the
  Tailscale fork.
  Declared in
  [../../../playbooks/group_vars/all/main.yml](../../../playbooks/group_vars/all/main.yml),
  which reads the store's `TAILSCALE_TAGS` with NO default: a missing value
  fails the join. The tag must be in the control server's `tagOwners` and the
  credential must be authorized for it.
- `tailscale_hostname` (the inventory hostname), `tailscale_accept_dns`
  (false: the host keeps its own resolver), `tailscale_ephemeral`,
  `tailscale_force_reauth`.

## Side effects

- Adds the Tailscale apt keyring + source, installs `tailscale`, enables
  `tailscaled`.
- `tailscale up` with the minted key (plus `--login-server` on the Headscale
  fork), handed over as a root-only temporary file the role removes after the
  join.
- Exposes the joined node's tailnet IPv4 as the `tailscale_ipv4` fact.
  `playbooks/lockdown.yml` runs this role first; from the panel's Lockdown it
  then closes public 22 behind it.

## Idempotency

`tailscale status --json` is read before anything is minted. A node already
in `Running` state is a no-op -- no key minted, no API call -- unless
`tailscale_force_reauth` is set, or the node is on another control server than
the stored provider names: its `tailscale debug prefs` `ControlURL` against
`tailnet_control_url` on Headscale, or against `tailscale_saas_control_urls` on
Tailscale. That join passes `--force-reauth`, without which `tailscale up`
refuses to move a running node to another control server.

## Related

- [../../../playbooks/rotate-tailscale.yml](../../../playbooks/rotate-tailscale.yml)
  -- explicit re-auth (runs this role with `tailscale_force_reauth: true`).
- [tasks/validate.yml](tasks/validate.yml) -- asserts package, keyring, apt
  source, `tailscaled` running + enabled, `tailscale0` present, and each
  configured tag in the live status JSON.
