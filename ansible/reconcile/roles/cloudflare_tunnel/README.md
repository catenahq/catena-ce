# cloudflare_tunnel

Dispatches the Cloudflare-tunnel converge to the host engine
`catena-cloudflared-sync`.

The tunnel find-or-create, wildcard DNS (`*.<zone>` ->
`<tunnel-id>.cfargotunnel.com`), ingress enforcement (single rule ->
`http://catena-traefik:80` plus a `404` fallback), the `cloudflared`
swarm service on `catena-network`, and the edge->tunnel->Traefik chain
probe live in that Go engine, whose source is in the private catena-admin
repository and whose contract is catena-admin
`payload/cmd/catena-cloudflared-sync/SYNC.md`. The binary ships in the
catena-admin host payload, and `reconcile/roles/payload` installs it to
`/usr/local/bin` earlier in the converge. The tunnel is a Community feature
and runs on every host; without a licence the engine serves the primary
domain only.

## What this role does

- Dispatches `catena-cloudflared-sync sync` on the host, unconditionally,
  passing the primary zone as `CLOUDFLARE_PRIMARY_ZONE`. The engine carries
  its own image, ingress, network and probe defaults, composes the tunnel
  name from the host's hostname and the store's prefix, and decides whether
  there is work to do.
- Prints the engine's summary line and its per-step detail.

The engine reads the token from `/etc/catena/config.json`; this role
passes none and does not check for one:

- **No token**: the engine prints `cloudflared-sync: skipped (no token)`
  and exits 0, so the tunnel is DEFERRED. The client enters the token in
  catena-admin > Settings; the panel validates it (`cloudflared-check`)
  and fires `cloudflared-sync` in the background. This is the shape of an
  install given no domain.
- **Token present**: the engine converges the tunnel. A
  present-but-invalid token exits nonzero and **aborts the converge**.
- **Engine not installed**: a converge that owns the payload
  (`CATENA_PAYLOAD_INSTALL=true`, the default) FAILS here rather than
  deferring, because deferring produces an `oauth2_proxy` failure two roles
  later against an edge nobody configured. A host whose engines were staged
  out of band defers.

## Position in the converge

The role runs after `reconcile/roles/payload` (the engine) and
`reconcile/roles/traefik` (`catena-network` and the ingress target), and
before `keycloak` and `oauth2_proxy`: their validation probes reach
`https://auth.<zone>/` through the tunnel. `infrastructure` runs after
`oauth2_proxy`, because its gated apps need their auth proxies and routes
rendered first, so the tunnel dispatch is a role of its own. The order is
`playbooks/converge.yml`'s, and `playbooks/reconcile.yml` keeps it.

## Dependencies

- The `catena-cloudflared-sync` engine in `/usr/local/bin`, installed by
  `reconcile/roles/payload`.
- On-box store token `cloudflare_api_token` (Zone:DNS:Edit +
  Cloudflare Tunnel:Edit on the target zone), entered with the zone in
  catena-admin > Settings.
- `catena-network` (provided by `reconcile/roles/traefik`) and an
  initialized Docker swarm (provided by `bootstrap/roles/docker`).

## Idempotency

`sync` is idempotent inside the engine (find-or-create, PUT-desired-
state, and the cloudflared token/network reconciles that avoid a
connector flap). The role reports the dispatch as unchanged so a
re-converge does not trip the changed=0 gate.

## Companion role

`reconcile/roles/cloudflare_tunnel_regenerate` is the rotation path, run by
the panel's Settings > Domain > Apply through `playbooks/rotate-tunnel.yml`
before its converge: it deletes the host's tunnel and its connectors through
the Cloudflare API, then includes this role, whose engine mints a new tunnel
from the stored token. It refuses when the store holds no Cloudflare token.
