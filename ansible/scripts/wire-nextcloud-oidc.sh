#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-oidc -- wire Keycloak as an
# OIDC provider in a deployed Nextcloud instance. Backs the
# "Wire Nextcloud OIDC" catena-admin action. Operator clicks it
# once after deploying Nextcloud from the app catalog; this registers the
# `keycloak` provider inside Nextcloud via `occ user_oidc:provider`.
#
# Idempotent: the occ command is itself an upsert (creates if missing,
# updates if present, per `occ user_oidc:provider --help`: "Create,
# show or update a OpenId connect provider config given the
# identifier"). Re-clicking the button after a secret rotation or env
# change picks up the new values. No `--upsert` flag is passed -- that
# flag does not exist in user_oidc 8.x.
#
# Why a button (not a converge task): a catalog deploy is not followed by
# a converge. A converge runs at install, on an update, from the panel, or
# on the scheduled converge lane, so wiring Nextcloud's OIDC the first time
# it is deployed cannot wait for one. Nextcloud's Keycloak client is its own
# sign-in entry: catena-admin's settings sync makes it from the template's
# vps.auth.oidc labels and writes its values into the stack env.

set -euo pipefail

ct=$(/usr/local/bin/catena-container --required catena-nextcloud app)
echo "Found Nextcloud container: $ct"

# The OIDC env is the sign-in entry's values, which the settings sync
# writes into the stack env and the template maps onto NEXTCLOUD_OIDC_*.
# Read it from inside the container so no secret has
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
    echo "These are the values of Nextcloud's own sign-in entry, which the" >&2
    echo "settings sync writes into the stack environment within a few" >&2
    echo "minutes of the first deploy, restarting Nextcloud once. Wait until" >&2
    echo "OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / OIDC_ISSUER_URL show in the" >&2
    echo "stack's Environment in Portainer, then run this action again." >&2
    exit 2
fi

DISCOVERY="${ISSUER_URL%/}/.well-known/openid-configuration"

echo "Wiring Keycloak as OIDC provider in Nextcloud..."
echo "  identifier:    keycloak"
echo "  client id:     $CLIENT_ID"
echo "  discovery uri: $DISCOVERY"
echo

# user_oidc is an app store app: app:enable downloads its newest release
# for this Nextcloud the first time, and each core upgrade moves it to the
# newest again. The provider command below is written for the release the
# template names in NEXTCLOUD_USER_OIDC_VERSION, so an installed release of
# another major version, or an older one, is refused before it runs.
want=$(get_env NEXTCLOUD_USER_OIDC_VERSION)
if [ -z "$want" ]; then
    echo "error: $ct names no NEXTCLOUD_USER_OIDC_VERSION; redeploy Nextcloud" \
         "from the catalog, then run this action again." >&2
    exit 2
fi
docker exec --user 33 "$ct" \
    php /var/www/html/occ app:enable user_oidc >/dev/null
have=$(docker exec --user 33 "$ct" \
    php /var/www/html/occ config:app:get user_oidc installed_version)
# sort -C exits 0 when its input is already in version order: want <= have.
if [ "${have%%.*}" != "${want%%.*}" ] \
    || ! printf '%s\n%s\n' "$want" "$have" | sort -V -C; then
    echo "error: user_oidc $have is installed; this action's provider command" \
         "is written for user_oidc $want (the same major version, $want or newer)." >&2
    exit 3
fi

# Idempotent provider upsert. The base command upserts; no --upsert
# flag exists in user_oidc 8.x. Mappings read the claims the realm's
# default client scopes put in the sign-in entry's tokens
# (preferred_username -> uid, groups -> groups, etc.).
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
