# keycloak

Provision Keycloak as the stack's IdP (Phase Two distribution).

## Steps

1. Create the `keycloak` role + database in `catena-postgres`
   (`provision_db.yml`).
2. Deploy the Keycloak compose as a swarm stack (`deploy.yml`) and wait
   for `/health/ready` (`validate.yml`), so the realm import and the
   downstream roles (oauth2_proxy) do not race its startup.
3. Bootstrap the `vps` realm (`realm_bootstrap.yml`) with
   keycloak-config-cli:
   - `realm-vps.yaml.j2` plus the service-account clients render into
     `/etc/catena/keycloak/realms` (parent dir 0700) and import on every
     converge: the four-tier groups (`admin`, `staff`, `client`,
     `visitor`), the security posture, MFA enforcement and the realm's
     mail settings, all from the on-box store.
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
