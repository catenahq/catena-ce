# cloudflare_tunnel_regenerate

Mints this host a new Cloudflare tunnel, replacing the one it has. Two
occasions: rotation when the tunnel credential is suspected compromised, and
recovery after the tunnel was deleted out of band in the Cloudflare dashboard.

## Two secrets, and which one this rotates

- **`TUNNEL_TOKEN`** -- the tunnel's own credential, carried in the cloudflared
  swarm service spec. This is what the role replaces.
- **`cloudflare_api_token`** -- the account credential that authenticates the
  API calls doing the replacing. The role reads it, never writes it. Rotating
  it is a separate act, done in catena-admin > Settings, and it has to land in
  the store before a rotation runs.

## Why it is its own role

`cloudflare_tunnel` converges the tunnel on every pass: find-or-create, DNS,
ingress, service, chain probe. Deleting is the one thing it must never do on
its own, because a converge able to delete the tunnel would drop the public
edge every run. The delete is an intent somebody declares by running the
playbook, which is why it lives here and is reachable from nowhere else.

## Inputs

None passed in. Everything is read from the on-box store
(`/etc/catena/config.json`), which the role slurps directly -- the playbook has
no `load_onbox_config` pre-task.

- `cloudflare_api_token` -- needs Zone:DNS:Edit + Account:Cloudflare Tunnel:Edit.
  The role refuses before deleting anything when the store holds none.
- `CLOUDFLARE_ZONE` -- the Cloudflare account id is not a separate input; it is
  resolved from the zone through the API on the same token.
- `CLOUDFLARED_TUNNEL_NAME_PREFIX` -- the tunnel name is composed the way
  `catena-cloudflared-sync` composes it: the box's own hostname with this
  prefix in front. The hostname is read from the host rather than from
  `inventory_hostname`, because the engine calls `os.Hostname()` and a play
  that guessed differently would rotate a tunnel that does not exist.

The API token is deliberately not an input. The re-create is a handoff to
`cloudflare_tunnel`, which dispatches `catena-cloudflared-sync`, and that
engine reads the store itself. A token handed to this role would reach the
delete and not the mint, so a run supplying anything other than the stored
token would revoke the tunnel and fail to replace it.

## Side effects

- Clears stale connectors, then deletes the prior CF Named Tunnel (if any).
  Cloudflare refuses to delete a tunnel that still has active connections.
- Creates a new tunnel and re-points the `*.<zone>` wildcard CNAME at it, via
  the `cloudflare_tunnel` handoff.
- Rolls the cloudflared swarm service onto the new `TUNNEL_TOKEN`, so the new
  credential takes effect without a converge. The credential lives in the
  service spec; nothing is written to disk.

## Idempotency

Not idempotent, by intent: every run revokes the current tunnel and mints
another. Listing by name first means a re-run replaces this host's tunnel
rather than accumulating tunnels. The handoff's own reconciles (DNS, ingress,
service) are idempotent and no-op when already correct.

## Related

- Entry point: `playbooks/regenerate-cf-tunnel.yml`, driven by
  `catena rotate-tunnel`.
- Converge counterpart: `reconcile/roles/cloudflare_tunnel`.
- Declared in `boundary.yml` under `own_playbook_roles`.
