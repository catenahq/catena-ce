# cloudflare_tunnel

Dispatches the Cloudflare-tunnel converge to the host engine
`catena-cloudflared-sync`.

The tunnel find-or-create, wildcard DNS (`*.<zone>` ->
`<tunnel-id>.cfargotunnel.com`), ingress enforcement (single rule ->
`http://catena-traefik:80` plus a `404` fallback), the `cloudflared`
swarm service on `catena-network`, and the edge->tunnel->Traefik chain
probe all now live in the Go engine `catena-cloudflared-sync`, whose
source is in the private catena-admin repository (a relative link out of
this public repo would dangle for every reader who has only this one).
That binary ships in the catena-admin host payload and
is installed to `/usr/local/bin` by `reconcile/roles/payload`, four roles ahead of
this one -- it is NOT license-gated (the Cloudflare tunnel is a
Community feature).

## What this role does now

- Dispatches `catena-cloudflared-sync sync` on the host, exporting the
  non-secret pins (image, tunnel name, ingress service, network,
  stop-grace, probe counts) from `defaults/main.yml`. Unconditionally:
  the engine decides whether there is work to do.
- Prints whatever the engine reported.

The token itself is never passed on the command line -- the engine reads
it from `/etc/catena/config.json`, and reads it whether or not this role
looked first. That is why the role no longer looks:

- **No token**: the engine prints `cloudflared-sync: skipped (no token)`
  and exits 0, so the tunnel is DEFERRED. The client enters the token in
  catena-admin > Settings; the panel validates it (`cloudflared-check`)
  and fires `cloudflared-sync` in the background.
- **Token present**: the engine converges the tunnel. A
  present-but-invalid token exits nonzero and **aborts the converge**
  (fail-fast, preserving the fi_n4/fi_s5 abort intent).

The role used to make the no-token call itself, from a fact
`tasks/load_onbox_config.yml` publishes. Two implementations of "is
there a token" can disagree, and this pair did: a play that skipped the
loader saw no token on a host that had one.

- **Engine not installed**: a converge that owns the payload
  (`CATENA_PAYLOAD_INSTALL=true`, the production default) FAILS here
  rather than deferring, because deferring produces an `oauth2_proxy`
  failure two roles later against an edge nobody configured. A host whose
  engines were staged out of band still defers.

## Why a separate role (not part of `infrastructure`)

Still runs BEFORE the SSO roles in site.yml. `oauth2_proxy` waits for
`https://auth.<zone>/.well-known/openid-configuration` to answer before
it deploys its compose; that URL is only reachable once the tunnel is
up. `infrastructure` runs AFTER `oauth2_proxy` (its gated apps need their
auth proxies and routes rendered first), so the tunnel dispatch can't ship
there.

## Site.yml position

```yaml
roles:
  - common
  - tailscale
  - storage
  - docker
  - payload           # installs the cloudflared-sync engine (host payload)
  - traefik           # creates catena-network + owns ingress
  - cloudflare_tunnel # <- this role (dispatches catena-cloudflared-sync)
  - keycloak
  - oauth2_proxy      # waits for auth.<zone>
  - infrastructure    # gated apps using oauth2_proxy chain
  - catena-admin
  - ...
```

## Dependencies

- The `catena-cloudflared-sync` engine on `/usr/local/bin` (installed by
  `reconcile/roles/payload`, four roles earlier).
- On-box store token `cloudflare_api_token` (Zone:DNS:Edit +
  Cloudflare Tunnel:Edit on the target zone) -- entered ONLY in
  catena-admin > Settings, never at install.
- `catena-network` exists (provided by `reconcile/roles/traefik`); Docker swarm
  initialized (provided by `bootstrap/roles/docker`).

## Idempotency

`sync` is idempotent inside the engine (find-or-create, PUT-desired-
state, and the cloudflared token/network reconciles that avoid a
connector flap). The role reports the dispatch as unchanged so a
re-converge does not trip the changed=0 gate.

## Companion role

`reconcile/roles/cloudflare_tunnel_regenerate` is the out-of-band rotation path --
it deletes the prior tunnel + connectors via the CF API, then
`include_role`s back into this role, which re-dispatches
`catena-cloudflared-sync sync` to recreate the tunnel from the stored
token. It refuses when the store holds no Cloudflare token.
