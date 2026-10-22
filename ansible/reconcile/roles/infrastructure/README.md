# infrastructure

The monitoring plane every host runs, the shared services the catalog apps
lean on, and the sync and probe wiring for the client apps deployed from the
catalog. `tasks/main.yml` is the map, in run order.

## Shared services (swarm stacks)

Deployed with `tasks/swarm_stack.yml` (`docker stack deploy`):

- **Gatus** (`gatus.<zone>`) -- endpoint monitoring. `00-base.yaml` is
  rendered here; `catena-gatus-sync` writes `50-catena-apps.yaml` from the
  app intent and `40-infra.yaml` from `templates/gatus-infra-spec.json.j2`,
  whose probes through the public edge are written once the edge serves a
  domain.
- **Healthchecks** (`healthchecks.<zone>`) -- the dead-man-switch plane the
  backup, the nightly chain, the offsite copy, the off-site heartbeat, the
  reboot-required probe and Beszel's alerts ping. Missed pings alert the admin's email, through the outgoing mail set in
  catena-admin, and the channels added in Healthchecks. This role seeds the
  email channel; catena-admin's catena-schedule keeps the scheduled lanes'
  checks on their schedule.
- **Beszel** (`beszel.<zone>`) -- hub and host agent for resource history and
  threshold alerts, with a small shim that forwards those alerts to
  Healthchecks.
Gatus, Healthchecks and Beszel's hub sit behind their own oauth2-proxy,
rendered by `reconcile/roles/oauth2_proxy` before this role runs, and each
one's only public route is its gate's.

Healthchecks takes the signed-in user from the `X-Forwarded-Email` header its
gate sets, from whoever sends it, so it is on `healthchecks_network` and not
on `catena-network`, where every client app is. Its gate, the panel, Gatus
(its alerts) and the Beszel shim join that network to reach it.

Beszel's hub signs in any realm user who reaches it, and its superuser's
password is the shared admin password, so the hub is on `beszel_network`
alone, with its gate and Gatus (its probe). The Beszel shim, whose
only caller is the hub, is there too, and on `healthchecks_network` to ping.
The hub's spec carries no credential: the seed makes the admin its first user
through its first-user endpoint on the loopback publish.

The Healthchecks and Beszel seeds read their values on stdin, never an argv or
environment. Healthchecks' credentials, the Beszel agent's token and the
shim's ping key are swarm secrets: a secret of theirs that no service mounts is
removed after the deploy.

## Data copies

`swarm_stack.yml` runs `catena-stack-update-managed infra-data copy` between
writing a stack file and deploying it. A stack whose `x-catena` block declares
data (`data.<service>.volume`, or `.database` for the databases of a swarm
service, as Keycloak's in catena-postgres) has that data copied, with its
service stopped for the copy, when the file changes the service's image; a
refusal fails the converge before the deploy. Gatus, Healthchecks and Beszel's
hub declare their volumes in their templates, and Keycloak its database in
`catena-ce/ansible/reconcile/roles/keycloak/templates/keycloak.compose.yml.j2`.
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
`healthchecks_*` and `beszel_*` secrets, and the SMTP and alert-channel
settings. dashboard-sync's env file also names the domains the
host serves, read from the tunnel engine's projection by
`playbooks/tasks/served_zones.yml`.

## Idempotency

- `docker stack deploy` sends the same spec every converge and swarm restarts
  nothing when it is unchanged; the stack file write carries the changed
  signal.
- The sync lanes' env files are templated with stable content.
