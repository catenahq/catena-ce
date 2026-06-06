#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-oidc -- wire Keycloak as an
# OIDC provider in a deployed Nextcloud instance. Backs the
# "Wire Nextcloud OIDC" catena-admin action. Operator clicks it
# once after deploying Nextcloud via Dokploy; this registers the
# `keycloak` provider inside Nextcloud via `occ user_oidc:provider`.
#
# Idempotent: the occ command is itself an upsert (creates if missing,
# updates if present, per `occ user_oidc:provider --help`: "Create,
# show or update a OpenId connect provider config given the
# identifier"). Re-clicking the button after a secret rotation or env
# change picks up the new values. No `--upsert` flag is passed -- that
# flag does not exist in user_oidc 8.x.
#
# Why a button (not a converge task): per project policy, converge
# runs only at initial install or full VPS repair. App-deploy
# lifecycle hooks belong in the catena-admin action layer. The Keycloak realm
# client `nextcloud` is still registered by converge (eagerly, before
# any Nextcloud deploy) -- that part is appropriate.

set -euo pipefail

# Locate the running Nextcloud app container. Dokploy compose names
# look like nextcloud-<hash>-app-<n>. Match the name prefix AND the
# compose service label: two name= filters are ORed by docker (they
# would also match -cron-/-db-/-redis-), but a name= plus a label= are
# different keys and get ANDed, pinning the app container exactly.
ct=$(docker ps \
    --filter 'name=nextcloud-' \
    --filter 'label=com.docker.compose.service=app' \
    --format '{{.Names}}' | head -n1)

if [ -z "$ct" ]; then
    echo "Nextcloud is not running on this host."
    echo
    echo "Deploy first: Dokploy UI -> Templates -> nextcloud-s3 -> Deploy."
    echo "Wait for the container to come up, then click this button again."
    exit 1
fi

echo "Found Nextcloud container: $ct"

# OIDC env was minted at deploy time from the catalog's
# env_managed_keys. Read it from inside the container so no secret has
# to travel through the host's argv or files. printenv exits 1 when
# the var is unset; `|| true` lets the validation block below report
# the missing var with a clear message instead of failing here.
get_env() {
    docker exec "$ct" /bin/sh -c "printenv \"$1\"" 2>/dev/null || true
}

CLIENT_ID=$(get_env NEXTCLOUD_OIDC_CLIENT_ID)
CLIENT_SECRET=$(get_env NEXTCLOUD_OIDC_CLIENT_SECRET)
ISSUER_URL=$(get_env NEXTCLOUD_OIDC_ISSUER_URL)

missing=()
[ -z "$CLIENT_ID" ]     && missing+=("NEXTCLOUD_OIDC_CLIENT_ID")
[ -z "$CLIENT_SECRET" ] && missing+=("NEXTCLOUD_OIDC_CLIENT_SECRET")
[ -z "$ISSUER_URL" ]    && missing+=("NEXTCLOUD_OIDC_ISSUER_URL")

if [ "${#missing[@]}" -gt 0 ]; then
    echo "error: missing required env on $ct:" >&2
    for m in "${missing[@]}"; do echo "  - $m" >&2; done
    echo >&2
    echo "These come from the Dokploy catalog env_managed_keys." >&2
    echo "Open Dokploy UI -> Templates -> nextcloud-s3 -> Edit -> Environment" >&2
    echo "and confirm OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / OIDC_ISSUER_URL" >&2
    echo "are set, then redeploy the service." >&2
    exit 2
fi

DISCOVERY="${ISSUER_URL%/}/.well-known/openid-configuration"

echo "Wiring Keycloak as OIDC provider in Nextcloud..."
echo "  identifier:    keycloak"
echo "  client id:     $CLIENT_ID"
echo "  discovery uri: $DISCOVERY"
echo

# Defensive: enable user_oidc. NC 33+ ships it enabled by default;
# this is a no-op when it already is. Older / re-imaged installs may
# need this.
docker exec --user 33 "$ct" \
    php /var/www/html/occ app:enable user_oidc >/dev/null

# Idempotent provider upsert. The base command upserts; no --upsert
# flag exists in user_oidc 8.x. Mappings match the Keycloak realm-
# nextcloud client (preferred_username -> uid, groups -> groups, etc.).
docker exec --user 33 "$ct" \
    php /var/www/html/occ user_oidc:provider keycloak \
        --no-interaction \
        --clientid="$CLIENT_ID" \
        --clientsecret="$CLIENT_SECRET" \
        --discoveryuri="$DISCOVERY" \
        --scope="openid email profile groups" \
        --mapping-uid=preferred_username \
        --mapping-display-name=name \
        --mapping-email=email \
        --mapping-groups=groups \
        --group-provisioning=1

echo
echo "✓ Keycloak wired as OIDC provider in Nextcloud."
echo
echo "Verify: open Nextcloud's login page -- you should see a"
echo "  'Log in with keycloak'"
echo "button. Click it, complete OIDC, land in your Nextcloud session."
