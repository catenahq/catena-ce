# infrastructure

The monitoring plane every host runs, the shared services the catalog apps
lean on, and the sync and probe wiring for the client apps deployed from the
catalog. `tasks/main.yml` is the map, in run order.

## Alert intake

`tasks/alert_intake.yml` deploys the stack `catena-alerts`: the service `intake`
(alias `catena-alert-intake`, user `nonroot`, port 8080) runs
`/usr/local/bin/catena-alert-intake` from the panel's image
(`catena_admin_image`), so a panel update moves it with the converge that follows.
It is on the overlay `alert_network` alone, which the default app that posts its
alerts to it joins (contracts `default-apps/wiring.json`, `alert_intake_sources`
says which app holds which source's token; `catena-uptime` posts with the status
token from the host), and keeps its state in the volume
`intake-state` at `/var/lib/catena-alert-intake`.

Its three bearer tokens are the minted store secrets `alert_intake_token_status`,
`alert_intake_token_resources` and `alert_intake_token_read`. Each is a swarm
secret named after its content and mounted at `/run/secrets/<store key>`, the file
the binary reads it from. The publish `127.0.0.1:18070` (host mode) is declared
loopback-only by the fragment `catena-alerts.json`; the alert hub on the host reads
the intake's state there. `tasks/validate.yml` checks that it answers `/health`
and refuses a request with no token.

## Default apps (catalog apps)

Beszel is the catalog app every host runs, in the app folder `catena-beszel`.
The list is `default_apps` in `playbooks/group_vars/all/main.yml` (app, first
tag, stack); the role writes it to `/etc/catena/default-apps.json` with each
app's public address (`default_app_addresses`, the gate hostnames). The
whole-server restore reads the file to leave their stacks running, and the
Apps page reads it to open each app at its address.
`tasks/default_app.yml` adds one with `catena-apps defaults -held -stdin` and
prints the engine's lines as they are; a tag that cannot be fetched (exit 3)
and exit 4 are printed, not failures. The request is `{app, tag, stack, env, run}`:
`env` holds the values this role owns (the contract's `env_core_writes`: the
addresses, the hub's sign-in header, its trusted subnet and realm), in
keys the app's block does not declare; `run` is `CATENA_INFRA_RUN`, under which
the engine files each move of the app (see Data copies). The names, ports, volumes and keys core
depends on are in contracts `default-apps/wiring.json`, which the unit tests
read. The address label of each app is its `SUBDOMAIN` setting, which
`playbooks/tasks/default_app_subdomains.yml` reads from the app's folder.

Each app sets itself up in its own configure hook; this role runs no seed. An
app an admin stopped or removed stays so. An app that leaves the list is left
alone on a host that runs it; `tasks/withdrawn.yml` removes only the files this
role once wrote for it (`infrastructure_withdrawn_files`), and `tasks/validate.yml`
asserts they are gone.

- **Beszel** (`beszel.<zone>`) -- one app holding the hub, the host agent
  (resource history and threshold alerts) and the relay that forwards those
  alerts to the alert intake. The relay's hook sets the hub up and hands the agent
  its key.

Beszel sits behind its own oauth2-proxy, rendered by
`reconcile/roles/oauth2_proxy` before this role runs, and its only
public route is its gate's.

Beszel's hub signs in any realm user who reaches it, so it is on `beszel_network`
and `beszel_gate_network` alone. Only the hub's gate joins `beszel_gate_network`.
The hub opens signed in as the email its gate sets (`TRUSTED_AUTH_HEADER`),
for peers inside that network's subnet alone (`TRUSTED_PROXY_IPS`): the
converge reads the subnet before the hub deploys and stops unless it is exactly
one valid IPv4 range, because Beszel trusts every peer when the list is empty.
An admin whose email has no hub record meets the Keycloak button once. Beszel's
own password login stays. Behind the gate the hub's Log out button signs no one
out; signing out of the gate does. Because the hub signs in any request that
carries no token, Traefik answers 403 to a browser `/api/` call whose
`Sec-Fetch-Site` is not `same-origin` (the gate's route file); the agents reach
the hub on the host loopback, not through Traefik.
The hub's spec carries no credential: the relay's hook makes the admin its
first user through the hub's first-user endpoint.

## Data copies

`swarm_stack.yml` runs `catena-stack-update-managed infra-data copy` between
writing a stack file and deploying it. A stack whose `x-catena` block declares
data (`data.<service>.volume`, or `.database` for the databases of a swarm
service, as Keycloak's in catena-postgres) has that data copied, with its
service stopped for the copy, when the file changes the service's image; a
refusal fails the converge before the deploy. Keycloak declares its database in
`catena-ce/ansible/reconcile/roles/keycloak/templates/keycloak.compose.yml.j2`;
Beszel declares its volumes in its catalog block,
and the update engine copies them as it does for any app.
A release update names the run the copies and the default apps' moves are
filed under (`CATENA_INFRA_RUN`); when it rolls back it puts the copies back
and reverts each default app it moved, with the copies the update engine kept.

## Catalog app wiring

- The public-URL probe of the gated services, and the intent-driven check that
  every gated client app denies an anonymous request.

## Sync lanes

`catena-dashboard-sync` (client-app routes and SSO) ships in the catena-admin
payload with its units. This role renders its env file and enables its timer.
`tasks/version_check.yml` pins whether the payload's `catena-version-check` is
on the host, for the version report's refresh at the end of the converge.

`catena-uptime` (catena-admin payload) checks every minute the addresses listed
in `/etc/catena/uptime-targets.json`, which `tasks/uptime.yml` renders from
`templates/uptime-targets.json.j2`: Catena's own services, and the Beszel hub
always. Its probes through the public edge are written once the edge serves a
domain. The check reads the applications off the app intent itself.

The off-site heartbeat (`catena-heartbeat`, catena-admin payload) probes the
Beszel hub and the alert intake on their loopback publishes and calls the
outside monitoring address saved in Monitoring > Alert settings. This role renders its
`/etc/catena/heartbeat.env`; catena-schedule enables its timer while the
`heartbeat` lane of Monitoring > Alert settings is on.

## Inputs

From the on-box store: `cloudflare_api_token` and the zone, the
`alert_intake_token_*` and `beszel_oidc_client_secret` secrets, and the alert-channel
settings. dashboard-sync's env file also names the domains the
host serves, read from the tunnel engine's projection by
`playbooks/tasks/served_zones.yml`.

## Idempotency

- `docker stack deploy` sends the same spec every converge and swarm restarts
  nothing when it is unchanged; the stack file write carries the changed
  signal.
- The sync lane's env file is templated with stable content.
