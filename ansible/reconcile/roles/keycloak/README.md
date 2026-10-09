# keycloak

Provision Keycloak as the stack's IdP (Phase Two distribution).

## Steps

1. Create the `keycloak` role + database in `catena-postgres`
   (`provision_db.yml`).
2. Deploy the Keycloak compose as a swarm stack (`deploy.yml`) and wait
   for `/health/ready` (`validate.yml`), so the realm import and the
   downstream roles (oauth2_proxy) do not race its startup. Its database
   password and initial admin password are a config file
   (`templates/keycloak.conf.j2`) mounted as a swarm secret named after its
   content and read through `KC_CONFIG_FILE`; a secret of that name no
   service mounts is removed after the deploy.
3. Bootstrap the `vps` realm (`realm_bootstrap.yml`) with
   keycloak-config-cli:
   - Before any import, an `admin_email` that differs from the one the last
     import ran under (`/var/lib/catena/keycloak-realm-admin-email`) moves
     the admin's master-realm and realm accounts to the new address by id,
     keeping their passwords and second factors: the import, which finds the
     admin by username, then updates that one account. Every sign-in the
     role makes reads the admin password on stdin, never an argv.
   - `realm-vps.yaml.j2` plus the service-account clients render into
     `/etc/catena/keycloak/realms` (parent dir 0700) and import on every
     converge: the four-tier groups (`admin`, `staff`, `client`,
     `visitor`) with their tier roles, the default client scopes, the
     two sign-in flows applications are bound to by tier, the security
     posture, MFA enforcement and the realm's mail settings, all from the
     on-box store.
   - After the import it reads the two sign-in flows' ids
     (`keycloak_signin_flows`), which dashboard-sync's env file carries,
     and fails the converge when either is missing.
   - The realm admin's password and the realm's tunable settings are a
     seed: rendered into `/etc/catena/keycloak/seed`, imported once while
     a bootstrap marker under `/var/lib/catena` is missing, then deleted.
   - Other roles render their own OIDC client next to the realm file
     and include `realm_bootstrap.yml` again.
4. Move members off the legacy `administrators`/`client-staff` groups,
   once (`migrate_groups.yml`).
5. Export the live realm to backup-staging on every converge
   (`realm_export.yml`).

## Inputs

- `admin_email`, `admin_password` -- the master-realm bootstrap admin
  and the seed of the realm's copy of that account.
- `keycloak_db_password` -- written into the catena-postgres user.
- `cfg_smtp_provider`, `cfg_smtp_host`, `cfg_smtp_port`, `cfg_smtp_user`,
  `cfg_smtp_sender` and `smtp_password` -- Settings > Mail, resolved by the
  `catena_smtp_resolve` filter (`defaults/main.yml`).
- `cfg_identity_enforce_mfa` -- Settings > Sign-in requirements.

## Idempotency

- DB provision uses CREATE-IF-NOT-EXISTS; password rotation via
  ALTER ROLE.
- The realm import is a merge with `IMPORT_MANAGED_*=NO_DELETE`: it
  creates and updates what the files declare and deletes nothing.

## Related

- Operator-facing: `ops/internal_docs/tools/keycloak-and-oauth2-proxy-gotchas.md`.
- Downstream: `oauth2_proxy` role.
