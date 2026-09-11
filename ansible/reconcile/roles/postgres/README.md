# postgres

The catena-owned Postgres for INFRA databases (Keycloak today). One
Postgres per VPS, multiple DBs inside; per-app databases (Nextcloud,
etc.) keep their own containers -- this hosts infra databases only.

## What it manages

- The `catena-postgres` swarm service, whose spec (image major, mount,
  env, hardening, placement, health probe) comes from the tier-1 host
  engine via `catena-tier1 spec catena-postgres` rather than from this
  role. The major follows Keycloak's support matrix and is pinned in
  that catalog.
- The superuser password as the create-once IMMUTABLE swarm secret
  `catena_postgres_password`, sourced from
  `catena_postgres_password` (minted on-box by the config
  loader). Kept out of `docker service inspect`. Rotation is a
  create-new-secret + service-update flow (deferred; no operator
  runbook yet).
- The `catena-postgres-data` named volume. Backed up RAW by the
  default restic set: the password comes from the (also-backed-up)
  on-box store and Postgres skips initdb on a restored non-empty
  volume, so a raw restore is password-consistent by construction --
  no post-restore reconciliation. (Exercised by the restore_dr +
  hot-restore bench scenarios.)
- A first-task guard that refuses the converge while
  `catena_postgres_password` is a placeholder -- BEFORE the
  create-once secret can be minted wrong (the fi_s3 bench scenario
  proves this fires ahead of any state change).

## Boundaries

- Database/user provisioning inside the instance belongs to the
  consumer roles (reconcile/roles/keycloak `provision_db.yml` detects the live
  superuser from the container env and follows this role's default).
- Backup/replay policy lives with reconcile/roles/backup; this role only shapes
  the volume so that policy stays raw-restore-correct.

## Key defaults

| Var | Default | Meaning |
| --- | --- | --- |
| `catena_postgres_service_name` | `catena-postgres` | swarm service + network alias |
| `catena_postgres_superuser` | `postgres` | consumer roles auto-detect |
| `catena_postgres_secret_name` | `catena_postgres_password` | create-once swarm secret |
| `catena_postgres_data_volume` | `catena-postgres-data` | raw-restored named volume |
