# infrastructure

The monitoring plane every host runs, the shared services the catalog apps
lean on, and the sync and probe wiring for the client apps deployed from the
catalog. `tasks/main.yml` is the map, in run order.

## Default apps (catalog apps)

Gatus, Healthchecks and Beszel are catalog apps every host runs, in the app
folders `catena-gatus`, `catena-healthchecks` and `catena-beszel`. The list
is `default_apps` in `playbooks/group_vars/all/main.yml` (app, first tag,
stack); the role writes it to `/etc/catena/default-apps.json`, which
catena-admin's whole-server restore reads to leave their stacks running.
`tasks/default_app.yml` adds one with `catena-apps defaults -held -stdin` and
prints the engine's lines as they are; a tag that cannot be fetched (exit 3)
and exit 4 are printed, not failures. The request is `{app, tag, stack, env}`:
`env` holds the values this role owns (the contract's `env_core_writes`: the
addresses, mail, the hub's sign-in header, its trusted subnet and realm), in
keys the app's block does not declare. The names, ports, volumes and keys core
depends on are in contracts `default-apps/wiring.json`, which the unit tests
read. The address label of each app is its `SUBDOMAIN` setting, which
`playbooks/tasks/default_app_subdomains.yml` reads from the app's folder.

Each app sets itself up in its own configure hook; this role runs no seed. An
app an admin stopped or removed stays so.

- **Gatus** (`gatus.<zone>`) -- endpoint monitoring. Its base service writes
  `00-base.yaml` into the app's config volume; `catena-gatus-sync` writes
  `50-catena-apps.yaml` from the app intent and `40-infra.yaml` from
  `templates/gatus-infra-spec.json.j2`, whose probes through the public edge
  are written once the edge serves a domain.
- **Healthchecks** (`healthchecks.<zone>`) -- the dead-man-switch plane the
  backup, the nightly chain, the offsite copy, the off-site heartbeat, the
  reboot-required probe and Beszel's alerts ping. Missed pings alert the admin's
  email, through the outgoing mail set in catena-admin, and the channels added
  in Healthchecks. The app's hook keeps the project and the email channel;
  catena-admin's catena-schedule keeps the scheduled lanes' checks on their
  schedule.
- **Beszel** (`beszel.<zone>`) -- one app holding the hub, the host agent
  (resource history and threshold alerts) and the relay that forwards those
  alerts to Healthchecks. The relay's hook sets the hub up and hands the agent
  its key.

Gatus, Healthchecks and Beszel sit behind their own oauth2-proxy,
rendered by `reconcile/roles/oauth2_proxy` before this role runs, and each
one's only public route is its gate's.

Healthchecks takes the signed-in user from the `X-Forwarded-Email` header its
gate sets, from whoever sends it, so it is on `healthchecks_network` and not
on `catena-network`, where every client app is. Its gate, the panel, Gatus
(its alerts) and Beszel's relay join that network to reach it.

Beszel's hub signs in any realm user who reaches it, so it is on `beszel_network`
and `beszel_gate_network` alone. Gatus (its probe) joins `beszel_network`.
Only the hub's gate joins `beszel_gate_network`.
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

Healthchecks' signing key and mail password are settings of its app, kept in the
app's `.env`.

## Data copies

`swarm_stack.yml` runs `catena-stack-update-managed infra-data copy` between
writing a stack file and deploying it. A stack whose `x-catena` block declares
data (`data.<service>.volume`, or `.database` for the databases of a swarm
service, as Keycloak's in catena-postgres) has that data copied, with its
service stopped for the copy, when the file changes the service's image; a
refusal fails the converge before the deploy. Keycloak declares its database in
`catena-ce/ansible/reconcile/roles/keycloak/templates/keycloak.compose.yml.j2`;
Gatus, Healthchecks and Beszel declare their volumes in their catalog blocks,
and the update engine copies them as it does for any app.
A release update names the run the copies are filed under (`CATENA_INFRA_RUN`)
and puts them back when it rolls back.

## Catalog app wiring

- The public-URL probe of the gated services, and the intent-driven check that
  every gated client app denies an anonymous request.

## Sync lanes

`catena-dashboard-sync` (client-app routes and SSO) and `catena-gatus-sync`
ship in the catena-admin payload with their units. This role renders their env
files and enables their timers.

The off-site heartbeat (`catena-heartbeat`, catena-admin payload) probes Gatus,
Healthchecks and the Beszel hub on their loopback publishes and calls the
outside monitoring address saved in Settings > Alerts. This role renders its
`/etc/catena/heartbeat.env`; catena-schedule enables its timer while the
Schedules page's `heartbeat` lane is on.

## Inputs

From the on-box store: `cloudflare_api_token` and the zone, the
`healthchecks_*` and `beszel_oidc_client_secret` secrets, and the SMTP and alert-channel
settings. dashboard-sync's env file also names the domains the
host serves, read from the tunnel engine's projection by
`playbooks/tasks/served_zones.yml`.

## Idempotency

- `docker stack deploy` sends the same spec every converge and swarm restarts
  nothing when it is unchanged; the stack file write carries the changed
  signal.
- The sync lanes' env files are templated with stable content.
