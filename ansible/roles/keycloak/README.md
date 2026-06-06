# keycloak

Provision Keycloak as the stack's IdP (Phase Two distribution).

## Steps

1. Create the `keycloak` role + database in `dokploy-postgres`
   (auxiliary task file: `provision_db.yml`).
2. Deploy the Keycloak compose project via the Dokploy API.
3. Bootstrap the `catena` realm:
   - Phase Two extensions (theme, password policy, account console
     v3, recovery codes).
   - Group seeding -- four-tier model: `admin`, `staff`, `client`,
     `visitor` (departments are subgroups of `/staff`). A one-time
     `migrate_groups.yml` moves members off the legacy
     `administrators`/`client-staff` groups on pre-rename tenants.
   - OIDC clients used by oauth2_proxy and direct-OIDC apps
     (Nextcloud, Rocket.Chat) -- created if missing, patched on
     drift.
4. Wait for `/health/ready` before returning so downstream roles
   (oauth2_proxy) don't race against startup.

## Inputs

- `vault_keycloak_admin_password` (KC_BOOTSTRAP_ADMIN_PASSWORD)
- `vault_keycloak_db_password` -- written into the dokploy-postgres
  user.
- `keycloak_realm_name` -- defaults to `catena`.
- `keycloak_oidc_clients` -- list of {client_id, redirect_uris,
  groups_claim} to seed.

## Idempotency

- DB provision uses CREATE-IF-NOT-EXISTS; password rotation via
  ALTER ROLE.
- Realm + clients managed by `keycloak-config-cli` against a JSON
  bundle -- drift-tolerant, no destructive change without explicit
  `keycloak_config_destroy=true`.

## Related

- Operator-facing: `internal_docs/operator/keycloak-and-oauth2-proxy-gotchas.md`.
- Downstream: `oauth2_proxy` role.
