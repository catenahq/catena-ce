#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-collabora -- wire Collabora CODE
# as the office editor inside a deployed Nextcloud instance. Backs the
# "Wire Nextcloud Collabora" catena-admin action. Operator runs it
# once after deploying the `collabora` template via Dokploy.
#
# Idempotent: re-clicking after a redeploy or config change converges
# to the same state. occ app:install no-ops when present;
# config:app:set overwrites unconditionally; app:remove no-ops when
# absent (we gate on app:list).
#
# Reversibility: if the OnlyOffice Nextcloud app (`onlyoffice`) is
# currently installed, this script removes it first and clears its
# residual app-config keys, then installs and configures
# `richdocuments` (the Nextcloud app for Collabora). Symmetric with
# catena-wire-nextcloud-onlyoffice; clicking either button leaves the
# OTHER editor's NC-side state cleanly removed.
#
# Probe-before-mutate: refuses to run if office.<base> is currently
# serving the OTHER editor (operator forgot to swap templates in
# Dokploy). Tells the operator how to fix it and exits non-zero
# without changing state. Net result: clicking the wrong button on
# top of the wrong template never corrupts NC config.
#
# Why a button (not a converge task): per project policy, converge
# runs only at initial install or full VPS repair. App-deploy
# lifecycle hooks belong in the catena-admin action layer.

set -euo pipefail

NC_ROOT=/var/www/html

# --- 1. Locate the running Nextcloud app container ----------------------
# Dokploy compose names look like nextcloud-<hash>-app-<n>. Match the
# name prefix AND the compose service label: two name= filters are ORed
# by docker (they would also match -cron-/-db-/-redis-), but a name=
# plus a label= are different keys and get ANDed, pinning the app
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

# Read env from inside the container so secrets do not travel through
# the host argv. Same pattern as wire-nextcloud-oidc.sh.
get_env() {
    docker exec "$ct" /bin/sh -c "printenv \"$1\"" 2>/dev/null || true
}

# --- 2. Resolve OFFICE_URL from NEXTCLOUD_HOSTNAME ----------------------
NC_HOSTNAME=$(get_env NEXTCLOUD_HOSTNAME)
if [ -z "$NC_HOSTNAME" ]; then
    echo "error: NEXTCLOUD_HOSTNAME is not set in the Nextcloud container env." >&2
    echo "       Check the Dokploy compose environment for nextcloud-s3." >&2
    exit 2
fi

# Drop the leading nextcloud. label to derive the base zone, mirroring
# nextcloud-talk-hpb-wire.sh (lines 47-54).
BASE=$(echo "$NC_HOSTNAME" | sed -e 's/^nextcloud\.//')
if [ -z "$BASE" ] || [ "$BASE" = "$NC_HOSTNAME" ]; then
    BASE="$NC_HOSTNAME"
fi
OFFICE_URL="https://office.$BASE"

# --- 3. Probe office.<base> to detect which editor is deployed ----------
# Internal aliases on dokploy-network:
#   - Collabora:  collabora:9980     /hosting/discovery -> XML <wopi-discovery>
#   - OnlyOffice: documentserver:80  /healthcheck       -> "true"
# Probe both from inside NC so the right error message can be rendered
# without running occ commands first.
exec_in_nc() {
    docker exec "$ct" /bin/sh -c "$1" 2>/dev/null
}

is_collabora_alive() {
    exec_in_nc 'curl -fsS --max-time 5 http://collabora:9980/hosting/discovery 2>/dev/null' \
        | grep -q '<wopi-discovery'
}

is_onlyoffice_alive() {
    exec_in_nc 'curl -fsS --max-time 5 http://documentserver/healthcheck 2>/dev/null' \
        | grep -qx 'true'
}

if is_onlyoffice_alive && ! is_collabora_alive; then
    cat >&2 <<EOF
error: this button wires Collabora, but office.$BASE is serving OnlyOffice.

To switch:
  1. Dokploy UI > Templates > onlyoffice > Stop
  2. Dokploy UI > Templates > collabora  > Deploy
  3. Re-click "Wire Nextcloud Collabora" here.

Nextcloud-side state was NOT changed. Safe to dismiss + retry once
the office template is the one you want.
EOF
    exit 3
fi

if ! is_collabora_alive; then
    cat >&2 <<EOF
error: Collabora is not reachable on the dokploy-network alias collabora:9980.

Check: Dokploy UI > Templates > collabora > Logs.
       The container should answer GET /hosting/discovery with XML.
       Wait ~30 s after Deploy for coolwsd to start, then retry.
EOF
    exit 4
fi

if is_onlyoffice_alive; then
    echo "warning: BOTH Collabora and OnlyOffice are running. Traefik may"
    echo "         pick either route for office.$BASE. Stop OnlyOffice in"
    echo "         Dokploy to avoid the conflict. Continuing with Collabora..."
fi

echo "Detected: Collabora at $OFFICE_URL (internal alias collabora:9980)"

# --- 4. Reverse OnlyOffice's NC-side state (idempotent) -----------------
# Gate on app presence to keep first-run output clean. `app:list
# --output=json` returns both enabled + disabled buckets in a single
# JSON document; a literal "\"onlyoffice\":" matches either bucket.
if docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:list --output=json \
        | grep -q '"onlyoffice":'; then
    echo "Removing the Nextcloud OnlyOffice app (reversal pole)..."
    docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:remove onlyoffice
    # Defensive: clear residual onlyoffice config keys. NC's app:remove
    # runs the app's own uninstall hook which clears most state, but
    # older OnlyOffice app versions ( < 9.x ) leave behind a few keys
    # that surface as stale defaults if the app is re-installed later.
    for k in DocumentServerUrl DocumentServerInternalUrl StorageUrl \
             jwt_secret editingMode defFormats editFormats sameTab \
             customizationGoback versionHistory; do
        docker exec --user 33 "$ct" \
            php "$NC_ROOT/occ" config:app:delete onlyoffice "$k" \
            >/dev/null 2>&1 || true
    done
    echo "  OnlyOffice Nextcloud app removed; residual config cleared."
fi

# --- 5. Install + enable richdocuments (Collabora's NC app) -------------
if docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:list --output=json \
        | grep -q '"richdocuments":'; then
    echo "richdocuments app already present; ensuring enabled..."
    docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:enable richdocuments >/dev/null
else
    echo "Installing richdocuments app from the Nextcloud appstore..."
    docker exec --user 33 "$ct" \
        php "$NC_ROOT/occ" app:install richdocuments
fi

# --- 6. Configure richdocuments to point at $OFFICE_URL -----------------
echo "Configuring richdocuments WOPI URL: $OFFICE_URL"
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" config:app:set richdocuments wopi_url \
        --value="$OFFICE_URL" >/dev/null
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" config:app:set richdocuments public_wopi_url \
        --value="$OFFICE_URL" >/dev/null
# Cloudflare Tunnel terminates the LE cert; coolwsd serves plain HTTP
# on 9980 and trusts ssl.termination=true. NC verifies the PUBLIC
# URL's cert end-to-end which is the correct posture; do not relax.
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" config:app:set richdocuments disable_certificate_verification \
        --value="no" >/dev/null

# --- 7. Trigger Collabora's discovery refresh ---------------------------
# `richdocuments:activate-config` (NC 28+) re-fetches /hosting/discovery
# from coolwsd and caches the WOPI handshake. On older NC the next
# file-open does the same lazily; either path converges.
docker exec --user 33 "$ct" \
    php "$NC_ROOT/occ" richdocuments:activate-config \
    || echo "  (activate-config not available on this NC; first file open will refresh discovery)"

echo
echo "Collabora wired in Nextcloud."
echo "  WOPI host:  $OFFICE_URL"
echo
echo "Verify: open any DOCX/XLSX/PPTX/ODT file in Nextcloud."
echo "        It should open in the embedded Collabora editor."
