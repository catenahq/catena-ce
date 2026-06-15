# cloudflare_tunnel_regenerate

Regenerate the Cloudflare tunnel credential via the Cloudflare API.
Used both for routine rotation and for recovery after the host's
tunnel gets orphaned (deleted on the CF side, missing locally).

This is split out from the `infrastructure` role because it is
intentionally an out-of-band path, gated by the operator (via the
portal "Regenerate tunnel" button or `regenerate-cf-tunnel.yml`)
rather than firing on every converge.

## Inputs

- `vault_cloudflare_api_token` -- token with `Cloudflare Tunnel:Edit`.
- `cloudflare_account_id` / `cloudflare_zone_id` -- host inventory.
- `cloudflare_tunnel_name` -- defaults to the Ansible inventory_hostname.

## Side effects

- Deletes the prior CF Named Tunnel (if any).
- Creates a new tunnel and re-points the `*.<zone>` wildcard CNAME
  at it (via the `cloudflare_tunnel` role's find-or-create handoff).
- Updates the cloudflared swarm service's `TUNNEL_TOKEN` env so the
  new credential takes effect immediately. The credential is carried
  in the swarm service spec, NOT written to a file on disk (there is
  no `/etc/cloudflared/` credentials JSON in this architecture).

## Idempotency

- Listing existing tunnels first means re-running with the same
  name is safe; the prior tunnel is deleted before the new one is
  created.
- The swarm `TUNNEL_TOKEN` env is reconciled only on real drift.

## Related

- Operator-facing: `runbooks/rotate-cloudflare-api.md`.
- Entry point: `playbooks/regenerate-cf-tunnel.yml`.
