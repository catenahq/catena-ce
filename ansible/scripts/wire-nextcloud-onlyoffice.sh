#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-onlyoffice -- wire OnlyOffice
# DocumentServer as the office editor inside a deployed Nextcloud
# instance. Backs the "Wire Nextcloud OnlyOffice" catena-admin action.
# Operator runs it once after deploying the `onlyoffice`
# template via Dokploy.
#
# Idempotent: re-clicking after a redeploy or JWT rotation converges
# to the same state. occ app:install no-ops when present;
# config:app:set overwrites unconditionally; app:remove no-ops when
# absent (we gate on app:list).
#
# Reversibility: if the Collabora Nextcloud app (`richdocuments`) is
# currently installed, this script removes it first and clears its
# residual app-config keys, then installs and configures `onlyoffice`
# (the Nextcloud app for OnlyOffice DocumentServer). Symmetric with
# catena-wire-nextcloud-collabora; clicking either button leaves the
# OTHER editor's NC-side state cleanly removed.
#
# Probe-before-mutate: refuses to run if office.<base> is currently
# serving the OTHER editor (operator forgot to swap templates in
# Dokploy). Tells the operator how to fix it and exits non-zero
# without changing state.
#
# JWT secret: read from the running documentserver container's env, so
# the secret never travels through host argv or files. The catalog
# mints JWT_SECRET via lookup('password', ...) at deploy time and
# Dokploy injects it into the container env -- this script just reads
# it back at wire time.

set -euo pipefail

NC_ROOT=/var/www/html

# --- 1. Locate the running Nextcloud app container ----------------------
# Match the name prefix AND the compose service label: two name= filters
# are ORed by docker (they would also match -cron-/-db-/-redis-), but a
# name= plus a label= are different keys and get ANDed, pinning the app
# container exactly.
ct=$(docker ps \
    --filter 'name=nextcloud-' \
    --filter 'label=com.docker.compose.service=app' \
    --format '{{.Names}}' | head -n1)

if [ -z "$ct" ]; then
    echo "Nextcloud is not running on this host."
    echo
    echo "Deploy first: Dokploy UI > Templates > nextcloud-s3 > Deploy."
    echo "Wait for the container to come up, then click this button again."
    exit 1
fi

echo "Found Nextcloud container: $ct"

get_env() {
    docker exec "$1" /bin/sh -c "printenv \"$2\"" 2>/dev/null || true
}

# --- 2. Resolve OFFICE_URL from NEXTCLOUD_HOSTNAME ----------------------
NC_HOSTNAME=$(get_env "$ct" NEXTCLOUD_HOSTNAME)
if [ -z "$NC_HOSTNAME" ]; then
    echo "error: NEXTCLOUD_HOSTNAME is not set in the Nextcloud container env." >&2
    echo "       Check the Dokploy compose environment for nextcloud-s3." >&2
    exit 2
fi

BASE=$(echo "$NC_HOSTNAME" | sed -e 's/^nextcloud\.//')
if [ -z "$BASE" ] || [ "$BASE" = "$NC_HOSTNAME" ]; then
    BASE="$NC_HOSTNAME"
fi
OFFICE_URL="https://office.$BASE"

# --- 3. Probe office.<base> to detect which editor is deployed ----------
# Internal aliases on dokploy-network:
#   - OnlyOffice: documentserver:80  /healthcheck       -> "true"
#   - Collabora:  collabora:9980     /hosting/discovery -> XML <wopi-discovery>
exec_in_nc() {
    docker exec "$ct" /bin/sh -c "$1" 2>/dev/null
}

is_onlyoffice_alive() {
    exec_in_nc 'curl -fsS --max-time 5 http://documentserver/healthcheck 2>/dev/null' \
        | grep -qx 'true'
}

is_collabora_alive() {
    exec_in_nc 'curl -fsS --max-time 5 http://collabora:9980/hosting/discovery 2>/dev/null' \
        | grep -q '<wopi-discovery'
}

if is_collabora_alive && ! is_onlyoffice_alive; then
    cat >&2 <<EOF
error: this button wires OnlyOffice, but office.$BASE is serving Collabora.

To switch:
  1. Dokploy UI > Templates > collabora  > Stop
  2. Dokploy UI > Templates > onlyoffice > Deploy
  3. Re-click "Wire Nextcloud OnlyOffice" here.

Nextcloud-side state was NOT changed. Safe to dismiss + retry once
the office template is the one you want.
EOF
    exit 3
fi

if ! is_onlyoffice_alive; then
    cat >&2 <<EOF
error: OnlyOffice is not reachable on the dokploy-network alias documentserver:80.

Check: Dokploy UI > Templates > onlyoffice > Logs.
       The container should answer GET /healthcheck with the literal "true".
       Wait ~1 min after Deploy for the document server to boot, then retry.
EOF
    exit 4
fi

if is_collabora_alive; then
    echo "warning: BOTH OnlyOffice and Collabora are running. Traefik may"
    echo "         pick either route for office.$BASE. Stop Collabora in"
    echo "         Dokploy to avoid the conflict. Continuing with OnlyOffice..."
fi

echo "Detected: OnlyOffice at $OFFICE_URL (internal alias documentserver:80)"

# --- 4. Read JWT_SECRET from the running documentserver container -------
# The catalog mints JWT_SECRET via lookup('password', ...) at deploy
# time; Dokploy injects it into the container env. Read it back here
# so the script stays stateless (no vault dependency, no host file).
ds=$(docker ps \
    --filter 'name=onlyoffice-' \
    --filter 'name=-documentserver-' \
    --format '{{.Names}}' | head -n1)

if [ -z "$ds" ]; then
    echo "error: documentserver container not found despite the healthcheck probe passing." >&2
    echo "       Check Dokploy UI > Templates > onlyoffice > status." >&2
    exit 5
fi

JWT_SECRET=$(get_env "$ds" JWT_SECRET)
if [ -z "$JWT_SECRET" ]; then
    echo "error: JWT_SECRET is not set in the documentserver container env." >&2
    echo "       Open Dokploy UI > Templates > onlyoffice > Edit > Environment" >&2
    echo "       and confirm JWT_SECRET is non-empty, then redeploy + retry." >&2
    exit 6
fi

# --- 5. Reverse Collabora's NC-side state (idempotent) ------------------
if docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:list --output=json \
        | grep -q '"richdocuments":'; then
    echo "Removing the Nextcloud Collabora app (richdocuments; reversal pole)..."
    docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:remove richdocuments
    # Defensive: clear residual richdocuments config keys. NC's
    # app:remove runs the app's own uninstall hook which clears most
    # state, but older richdocuments versions ( < 8.4 ) leave behind
    # a few keys that surface as stale defaults if the app is re-
    # installed later.
    for k in wopi_url public_wopi_url wopi_callback_url \
             external_apps disable_certificate_verification \
             secret edit_groups read_only_groups; do
        docker exec --user 33 "$ct" \
            php "$NC_ROOT/occ" config:app:delete richdocuments "$k" \
            >/dev/null 2>&1 || true
    done
    echo "  Collabora Nextcloud app removed; residual config cleared."
fi

# --- 6. Install + enable onlyoffice (NC app) ----------------------------
if docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:list --output=json \
        | grep -q '"onlyoffice":'; then
    echo "onlyoffice app already present; ensuring enabled..."
    docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:enable onlyoffice >/dev/null
else
    echo "Installing onlyoffice app from the Nextcloud appstore..."
    docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:install onlyoffice
fi

# --- 7. Configure onlyoffice to point at $OFFICE_URL --------------------
# DocumentServerUrl is the URL the user's BROWSER hits for the editor
# iframe; DocumentServerInternalUrl is the URL the NC server-side
# WOPI client hits for callbacks. They are identical for catena
# (everything goes through the public CF tunnel) -- the OnlyOffice NC
# app accepts that.
echo "Configuring onlyoffice DocumentServerUrl: $OFFICE_URL/"
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" config:app:set onlyoffice DocumentServerUrl \
        --value="$OFFICE_URL/" >/dev/null
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" config:app:set onlyoffice DocumentServerInternalUrl \
        --value="$OFFICE_URL/" >/dev/null
# StorageUrl="" forces NC to derive the storage URL from its own
# overwrite.cli.url (the NC's public URL), which is what we want.
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" config:app:set onlyoffice StorageUrl \
        --value="" >/dev/null
# JWT pass-through. Read from the running documentserver container's
# env (step 4) so the secret never appears in this script's argv on
# the host.
docker exec --user 33 -e JWT_SECRET="$JWT_SECRET" "$ct" \
    sh -c "php $NC_ROOT/occ config:app:set onlyoffice jwt_secret --value=\"\$JWT_SECRET\"" \
    >/dev/null

# --- 8. Verify the round-trip --------------------------------------------
# OnlyOffice has no per-app verify command analogous to Collabora's
# `richdocuments:activate-config`; the next file-open performs the
# JWT-signed handshake. Re-probe /healthcheck as a sanity check so the
# operator gets a green light in this run rather than at first user
# document open.
if exec_in_nc 'curl -fsS --max-time 5 http://documentserver/healthcheck 2>/dev/null' \
        | grep -qx 'true'; then
    echo "  documentserver /healthcheck returned 'true' from inside Nextcloud."
fi

echo
echo "OnlyOffice wired in Nextcloud."
echo "  DocumentServerUrl: $OFFICE_URL/"
echo "  JWT:               configured (secret read from documentserver container env)"
echo
echo "Verify: open any DOCX/XLSX/PPTX file in Nextcloud."
echo "        It should open in the embedded OnlyOffice editor."
