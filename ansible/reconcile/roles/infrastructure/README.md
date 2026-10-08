# infrastructure

The monitoring plane every host runs, the shared services the catalog apps
lean on, and the wiring that finishes those apps once a client deploys them.
`tasks/main.yml` is the map, in run order.

## Shared services (swarm stacks)

Deployed with `tasks/swarm_stack.yml` (`docker stack deploy`), with no
Portainer in the path, so a Portainer that will not start leaves them running:

- **Gatus** (`gatus.<zone>`) -- endpoint monitoring. `00-base.yaml` is
  rendered here; `catena-gatus-sync` writes `50-catena-apps.yaml` from the
  running containers' labels.
- **Healthchecks** (`healthchecks.<zone>`) -- the dead-man-switch plane the
  backup, the reboot-required probe, the ClamAV watch, the mail canary and
  Beszel's alerts ping. Missed pings alert the admin's email, through the
  outgoing mail set in catena-admin, and the ntfy channel set there.
- **Beszel** (`beszel.<zone>`) -- hub and host agent for resource history and
  threshold alerts, with a small shim that forwards those alerts to
  Healthchecks.
- **Shared ClamAV** -- one clamd on the `catena-clamav` overlay for the mail
  server and Nextcloud, deployed once either runs, with its watchdog timer.
  A server with 8 GB or more reloads clamd's signatures with scans running.
  A smaller one has less memory than `clamav_blocking_reload_below_mb`: clamd
  reloads with scans paused for the seconds the load takes, rather than
  holding a second copy of the database. Mail that arrives in the pause is deferred and retried by its
  sender; a Nextcloud upload waits for its scan, and one that gets no answer
  is refused.

Gatus, Healthchecks and Beszel's hub sit behind their own oauth2-proxy,
rendered by `reconcile/roles/oauth2_proxy` before this role runs.

## Catalog app wiring

Gated on the Portainer API key (`reconcile/roles/portainer` mints it):

- The Keycloak clients for Nextcloud and Element, and the host scripts behind
  the panel's "Initial apps setup" actions (`/usr/local/bin/catena-wire-*`),
  which finish Nextcloud (OIDC, Talk + HPB, office editors, antivirus, Mail)
  and Rocket.Chat's Jitsi inside the running containers.
- The mail server chain when its template is deployed: the DNS-01 certificate,
  the postmaster mailbox, Roundcube and Dovecot OIDC, rspamd filtering, the
  mail DNS records and the mail canary timer. It waits for a Cloudflare token.
- WordPress plugin curation once the site is installed.
- A health gate on Portainer's local environment (`tasks/portainer_endpoint.yml`).
- The public-URL probe of the gated services, and the intent-driven check that
  every gated client app denies an anonymous request.

## Sync lanes

`catena-dashboard-sync` (client-app routes and SSO) and `catena-gatus-sync`
ship in the catena-admin payload with their units. This role renders their env
files and enables their timers.

## Inputs

From the on-box store: `cloudflare_api_token` and the zone, the
`healthchecks_*` and `beszel_*` secrets, `portainer_api_key`, and the SMTP and
alert-channel settings. dashboard-sync's env file also names the domains the
host serves, read from the tunnel engine's projection by
`playbooks/tasks/served_zones.yml`.

## Idempotency

- `docker stack deploy` sends the same spec every converge and swarm restarts
  nothing when it is unchanged; the stack file write carries the changed
  signal.
- The sync lanes' env files are templated with stable content.
