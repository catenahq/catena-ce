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
   service mounts is removed after the deploy. Beside it, the Catena extension
   JAR (`provider/`, built into the catena-admin image and carried in
   `files/catena-keycloak.jar`) is copied to `/var/lib/catena/keycloak` and
   mounted in Keycloak's providers directory from a swarm config named after
   its content, removed the same way once no service mounts it; the deploy
   stops when the JAR is missing or over 500 KB, as it is in any tree not taken
   out of an image.
3. Bootstrap the `vps` realm (`realm_bootstrap.yml`) with
   keycloak-config-cli:
   - Every import and kcadm session signs in to the master realm as the
     automation client, `catena-automation` (`_master_login.yml`), a service
     account holding the master realm's admin role, with the secret
     `keycloak_automation_client_secret`. While that
     client cannot sign in and read the master realm, the role creates it, or
     gives it the store's secret and the admin role, signing in with the
     admin's password once per address per attempt and stopping at a refusal,
     which counts toward the master realm's lockout; that works until the
     master admin has a second factor, which is asked for only once the client
     exists.
   - Before any import, an `admin_email` that differs from the one the last
     import ran under (`/var/lib/catena/keycloak-realm-admin-email`) moves
     the admin's master-realm and realm accounts to the new address by id,
     keeping their passwords and second factors: the realm seed keys the
     realm account on it, and the master realm's second factor is asked of
     the master account named by it.
   - `realm-vps.yaml.j2` plus the service-account clients render into
     `/etc/catena/keycloak/realms` (parent dir 0700) and import on every
     converge: the four-tier groups (`admin`, `staff`, `client`,
     `visitor`), the `groups` client scope, the
     two sign-in flows applications are bound to by tier with a client scope
     named after each, the post-broker-login flow, the security
     posture and MFA enforcement, all from the on-box store, and the realm's
     mail settings from it while the realm holds the ones Catena wrote
     (below).
   - After the import, with kcadm, it turns the master realm's lockout on (a
     growing pause after repeated wrong passwords, capped at 15 minutes, as in
     this realm) and asks the master admin to set up a one-time code at their
     next sign-in while they hold none, and, while Settings requires a second
     factor, every other master-realm user holding the admin role (the
     automation client's service account excepted; see "Lifting a master-realm
     lockout or a lost second factor").
   - Then, adding only, it creates the tier roles the two flows check
     (`catena-tier-admin`, `catena-tier-staff`) and grants each to its group
     (`/admin`, `/staff`), adds the scopes applications need to the realm's
     default client scopes, puts the realm admin back in `/admin` when
     `/admin` has no member, gives the realm admin the `realm-admin` role of
     the realm-management client once (the realm's own admin console opens
     for it; the seed gives it to a new realm, and a marker under
     `/var/lib/catena` keeps a role removed by hand removed), and fails the converge naming each identity
     provider that names no post-broker-login flow. It reports a realm switched off,
     moves the sign-in page name to a new domain (below), then reads the two
     sign-in flows' ids (`keycloak_signin_flows`), which dashboard-sync's env
     file carries, and fails the converge when either is missing.
   - An account signing in through an external identity provider finishes in
     Keycloak's broker flows, never in an application's sign-in flow, so
     `catena-post-broker-login` runs its tier check after the provider's
     sign-in: it denies an account outside the tier of the entry it signs in
     to, which dashboard-sync marks with the client scope named after the
     entry's flow (`catena-signin-admin`, `catena-signin-staff`). Both checks
     end in the extension's `catena-tier-refusal` step, which refuses without
     counting toward the lockout. The extension's `catena-tier-binding` names
     the flow on each provider that names none, when an administrator creates,
     updates or imports one and for the providers present when Keycloak starts;
     the converge first checks that Keycloak lists both providers.
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
- `admin_first_name`, `admin_last_name` -- the administrator's name, written
  into both accounts while their names are empty (`realm_bootstrap.yml`).
- `keycloak_automation_client_secret` -- the automation client's secret,
  minted on the box.
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
  - the two sign-in flows, the post-broker-login flow and their step
    settings, which decide who may sign in to an application limited to staff
    or administrators;
  - the `groups` client scope's mapper: the group claim every gate compares;
  - the settings each client file declares for its own client, its return
    addresses among them, and each service account's client roles, which
    hold it to least privilege;
  - the MFA required action and the security posture, which the panel's
    Settings own;
  - the master realm's lockout switch, and its admin's second factor while
    they hold none (every master admin's, while Settings requires one).
- Everything else survives a converge: hand-made roles, groups, departments,
  clients, flows and their settings, roles mapped to `/admin` or `/staff` or
  added to a tier role, scopes added to the realm's default client scopes,
  the realm admin's account, and the realm's Enabled switch: a realm an
  administrator switched off stays off, and the converge completes on it and
  says so.
- The realm admin's `realm-admin` role is given once, by the seed or by the
  converge step that writes `keycloak_realm_admin_role_marker`; a role removed
  by hand afterwards stays removed.
- The administrator's first and last name are written into the admin's account
  in the master realm and in the realm while that name is empty; a name set in
  Keycloak stays. A name the realm's profile refuses is a warning.
- The sign-in page name (`displayName` and `displayNameHtml`) follows the
  domain while it still holds the name Catena wrote, which the realm records
  in its `catenaDisplayName` attribute: a converge with another domain writes
  the new name and the new record. A name given in Keycloak's realm settings
  stays. On a realm seeded before the record, a name equal to the seed's for
  the current domain, or the seed's empty one from a host with no domain yet,
  counts as Catena's (`playbooks/filter_plugins/keycloak_signin_name.py`).
- The realm's mail settings follow Settings > Outgoing mail while they are the
  ones Catena wrote, which the realm records in its `catenaMailSettings`
  attribute as a fingerprint of the settings Keycloak returns (the stored
  password is not among them). Before the imports the converge reads the
  realm's settings: it writes the panel's when the realm holds none, holds no
  record yet (the realm is adopted, whatever it holds, and the record follows),
  or holds the record's. Mail settings changed in the realm's Email tab after
  that stay: the import declares none, keycloak-config-cli sends no
  `smtpServer` and Keycloak keeps the realm's, and the converge says so.
  Handing them back to Settings is described below. A hand edit that changes
  only the password cannot be seen, and the next converge writes the panel's
  password over it (`playbooks/filter_plugins/keycloak_mail_settings.py`).

## Lifting a master-realm lockout or a lost second factor

As root on the server, sign kcadm in as the automation client, its secret
read from the store into `KC_CLI_CLIENT_SECRET`, where kcadm finds it, and find
the master admin's id:

```sh
ct=$(docker ps -q -f name=keycloak_server)
store() { python3 -c 'import json, sys; print(json.load(open("/etc/catena/config.json"))[sys.argv[1]][sys.argv[2]])' "$@"; }
export KC_CLI_CLIENT_SECRET=$(store secrets keycloak_automation_client_secret)
kc() { docker exec -e KC_CLI_CLIENT_SECRET "$ct" /opt/keycloak/bin/kcadm.sh "$@"; }
kc config credentials --server http://localhost:8080 --realm master --client catena-automation
id=$(kc get users -r master -q "username=$(store config ADMIN_EMAIL)" -q exact=true --fields id --format csv --noquotes)
```

Then lift the lockout:

```sh
kc delete "attack-detection/brute-force/users/$id" -r master
```

or remove a lost one-time code (`get "users/$id/credentials"` lists it with
type `otp`); the next converge asks the admin to set up a new one:

```sh
kc delete "users/$id/credentials/<credential id>" -r master
```

## Handing the realm's mail settings back to Settings

The Email tab of the realm's settings in Keycloak's admin console has no control
to clear its fields. With kcadm signed in as in the first block above, empty the
realm's mail settings; the next converge writes the ones Settings > Outgoing
mail shows and records them:

```sh
kc update realms/vps -s 'smtpServer={}'
```

Setting the Email tab to exactly the values Settings > Outgoing mail shows works
too: the converge after it records them.

## Related

- Operator-facing: `ops/internal_docs/tools/keycloak-and-oauth2-proxy-gotchas.md`.
- Downstream: `oauth2_proxy` role.
