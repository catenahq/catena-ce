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
     keeping their passwords and second factors: every import and kcadm
     session signs in to the master realm as `admin_email`, and the realm
     seed keys the realm account on it. Every sign-in the role makes reads
     the admin password on stdin, never an argv.
   - `realm-vps.yaml.j2` plus the service-account clients render into
     `/etc/catena/keycloak/realms` (parent dir 0700) and import on every
     converge: the four-tier groups (`admin`, `staff`, `client`,
     `visitor`), the `groups` client scope, the
     two sign-in flows applications are bound to by tier, the security
     posture, MFA enforcement and the realm's mail settings, all from the
     on-box store.
   - After the import, with kcadm and adding only, it creates the tier roles
     the two flows check (`catena-tier-admin`, `catena-tier-staff`) and grants
     each to its group (`/admin`, `/staff`), adds the scopes applications need
     to the realm's default client scopes, and puts the realm admin back in
     `/admin` when `/admin` has no member. It reports a realm switched off,
     moves the sign-in page name to a new domain (below), then reads the two
     sign-in flows' ids (`keycloak_signin_flows`), which dashboard-sync's env
     file carries, and fails the converge when either is missing.
   - The realm admin's account and the realm's tunable settings are a seed:
     rendered into `/etc/catena/keycloak/seed`, imported while a bootstrap
     marker under `/var/lib/catena` or the realm itself is missing, then
     deleted. The seed that creates the realm also switches it on.
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
- The realm import (`tasks/_config_cli_import.yml`) creates and updates what
  the files declare and deletes nothing (`IMPORT_MANAGED_*=NO_DELETE`); a
  declared user keeps the groups and realm roles given to it by hand
  (`IMPORT_USERS_MERGE_*`). Inside a declared object it is exact, so the files
  declare only what Catena owns, and the next converge reverts a hand edit
  there:
  - the two sign-in flows and their step settings, which decide who may sign
    in to an application limited to staff or administrators;
  - the `groups` client scope's mapper: the group claim every gate compares;
  - the settings each client file declares for its own client, its return
    addresses among them, and each service account's client roles, which
    hold it to least privilege;
  - the MFA required action, the mail settings and the security posture,
    which the panel's Settings own.
- Everything else survives a converge: hand-made roles, groups, departments,
  clients, flows and their settings, roles mapped to `/admin` or `/staff` or
  added to a tier role, scopes added to the realm's default client scopes,
  the realm admin's account, and the realm's Enabled switch: a realm an
  administrator switched off stays off, and the converge completes on it and
  says so.
- The sign-in page name (`displayName` and `displayNameHtml`) follows the
  domain while it still holds the name Catena wrote, which the realm records
  in its `catenaDisplayName` attribute: a converge with another domain writes
  the new name and the new record. A name given in Keycloak's realm settings
  stays. On a realm seeded before the record, a name equal to the seed's for
  the current domain, or the seed's empty one from a host with no domain yet,
  counts as Catena's (`playbooks/filter_plugins/keycloak_signin_name.py`).

## Related

- Operator-facing: `ops/internal_docs/tools/keycloak-and-oauth2-proxy-gotchas.md`.
- Downstream: `oauth2_proxy` role.
