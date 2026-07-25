# cloudflare_tunnel

Token-gated dispatch of the Cloudflare-tunnel converge to the host
engine `catena-cloudflared-sync`.

The tunnel find-or-create, wildcard DNS (`*.<zone>` ->
`<tunnel-id>.cfargotunnel.com`), ingress enforcement (single rule ->
`http://catena-traefik:80` plus a `404` fallback), the `cloudflared`
swarm service on `catena-network`, and the edge->tunnel->Traefik chain
probe all now live in the Go engine `catena-cloudflared-sync`, whose
source is in the private catena-admin repository (a relative link out of
this public repo would dangle for every reader who has only this one).
That binary ships in the catena-admin host payload and
is installed to `/usr/local/bin` on **every** converge by
`roles/catena-admin` -- it is NOT license-gated (the Cloudflare tunnel
is a Community feature).

## What this role does now

- Reads the on-box Cloudflare API token
  (`vault_cloudflare_api_token`, published as a fact by
  `tasks/load_onbox_config.yml`).
- **No token**: the tunnel is DEFERRED. The role logs a skip. The client
  enters the token in catena-admin > Settings; the panel validates it
  (`cloudflared-check`) and fires `cloudflared-sync` in the background.
- **Token present**: dispatches `catena-cloudflared-sync sync` on the
  host, exporting the non-secret pins (image, tunnel name, ingress
  service, network, stop-grace, probe counts) from `defaults/main.yml`.
  The token itself is never passed on the command line -- the engine
  reads it from `/etc/catena/config.json`. A present-but-invalid token
  exits nonzero and **aborts the converge** (fail-fast, preserving the
  fi_n4/fi_s5 abort intent).
- **Engine not installed yet** (first converge, before `roles/catena-admin`
  installs the payload; or a bench that stages the payload out of band):
  the role skips gracefully -- the tunnel converges on the next run or
  via the panel-triggered `cloudflared-sync`.

## Why a separate role (not part of `infrastructure`)

Still runs BEFORE the SSO roles in site.yml. `oauth2_proxy` waits for
`https://auth.<zone>/.well-known/openid-configuration` to answer before
it deploys its compose; that URL is only reachable once the tunnel is
up. `infrastructure` runs AFTER `oauth2_proxy` (its gated apps need the
forward-auth chain rendered first), so the tunnel dispatch can't ship
there.

## Site.yml position

```yaml
roles:
  - common
  - tailscale
  - storage
  - docker
  - traefik           # creates catena-network + owns ingress
  - cloudflare_tunnel # <- this role (dispatches catena-cloudflared-sync)
  - keycloak
  - oauth2_proxy      # waits for auth.<zone>
  - infrastructure    # gated apps using oauth2_proxy chain
  - catena-admin      # installs the cloudflared-sync engine (host payload)
  - ...
```

## Dependencies

- The `catena-cloudflared-sync` engine on `/usr/local/bin` (installed by
  `roles/catena-admin`).
- On-box store token `vault_cloudflare_api_token` (Zone:DNS:Edit +
  Cloudflare Tunnel:Edit on the target zone) -- entered ONLY in
  catena-admin > Settings, never at install.
- `catena-network` exists (provided by `roles/traefik`); Docker swarm
  initialized (provided by `roles/docker`).

## Idempotency

`sync` is idempotent inside the engine (find-or-create, PUT-desired-
state, and the cloudflared token/network reconciles that avoid a
connector flap). The role reports the dispatch as unchanged so a
re-converge does not trip the changed=0 gate.

## Companion role

`roles/cloudflare_tunnel_regenerate` is the out-of-band rotation path --
it deletes the prior tunnel + connectors via the CF API, then
`include_role`s back into this role, which re-dispatches
`catena-cloudflared-sync sync` to recreate the tunnel from the stored
token. It refuses when the store holds no Cloudflare token.
