#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-office <collabora|onlyoffice> -- wire
# one office editor into a deployed Nextcloud instance. Backs the "Wire
# Nextcloud Collabora" and "Wire Nextcloud OnlyOffice" catena-admin actions;
# the operator runs the one matching the office template deployed from the
# app catalog (both serve office.<base>, so one is deployed at a time).
#
# Idempotent: re-running after a redeploy, a config change or a JWT rotation
# converges to the same state. occ app:install no-ops when present;
# config:app:set overwrites unconditionally; the other editor's app is
# removed only when app:list shows it.
#
# Reversibility: the OTHER editor's Nextcloud app is removed first, with its
# residual app-config keys, then this editor's app is installed and
# configured. Running either action leaves the other editor's Nextcloud-side
# state cleanly removed.
#
# Probe-before-mutate: refuses to run if office.<base> is serving the OTHER
# editor (the templates need swapping in Portainer), says how to fix it, and
# exits non-zero without changing state. The wrong action on the wrong
# template never corrupts Nextcloud config.
#
# OnlyOffice's JWT secret is read from the running documentserver container's
# env, so it never travels through host argv or files: the catalog mints
# JWT_SECRET at deploy time and the stack env injects it.

set -euo pipefail

EDITOR="${1:-}"
case "$EDITOR" in
    collabora)  NAME=Collabora;  OTHER=onlyoffice; OTHER_NAME=OnlyOffice ;;
    onlyoffice) NAME=OnlyOffice; OTHER=collabora;  OTHER_NAME=Collabora ;;
    *)
        echo "usage: $0 collabora|onlyoffice" >&2
        exit 2
        ;;
esac

NC_ROOT=/var/www/html

ct=$(/usr/local/bin/catena-nextcloud-container --required)
echo "Found Nextcloud container: $ct"

get_env() {
    docker exec "$1" /bin/sh -c "printenv \"$2\"" 2>/dev/null || true
}

occ() { docker exec --user 33 "$ct" php "$NC_ROOT/occ" "$@"; }

# The Nextcloud app each editor runs under, and the app-config keys its
# app:remove can leave behind: the uninstall hook clears most state, but
# older releases of both apps leave a few keys that surface as stale defaults
# on a later re-install.
nc_app() {
    case "$1" in
        collabora)  echo richdocuments ;;
        onlyoffice) echo onlyoffice ;;
    esac
}
residual_keys() {
    case "$1" in
        collabora)  echo wopi_url public_wopi_url wopi_callback_url external_apps \
                         disable_certificate_verification secret edit_groups \
                         read_only_groups ;;
        onlyoffice) echo DocumentServerUrl DocumentServerInternalUrl StorageUrl \
                         jwt_secret editingMode defFormats editFormats sameTab \
                         customizationGoback versionHistory ;;
    esac
}

# `app:list --output=json` returns both enabled + disabled buckets in a single
# JSON document; a literal "\"<app>\":" matches either bucket.
nc_has_app() {
    occ app:list --output=json | grep -q "\"$1\":"
}

# --- 1. Resolve OFFICE_URL from NEXTCLOUD_HOSTNAME ----------------------
NC_HOSTNAME=$(get_env "$ct" NEXTCLOUD_HOSTNAME)
if [ -z "$NC_HOSTNAME" ]; then
    echo "error: NEXTCLOUD_HOSTNAME is not set in the Nextcloud container env." >&2
    echo "       Check the stack environment for nextcloud-s3 in Portainer." >&2
    exit 2
fi

# Drop the leading nextcloud. label to derive the base zone.
BASE=$(echo "$NC_HOSTNAME" | sed -e 's/^nextcloud\.//')
if [ -z "$BASE" ] || [ "$BASE" = "$NC_HOSTNAME" ]; then
    BASE="$NC_HOSTNAME"
fi
OFFICE_URL="https://office.$BASE"

# --- 2. Probe office.<base> to detect which editor is deployed ----------
# Internal aliases on catena-network, probed from inside Nextcloud so the
# right error can be given before any occ command runs:
#   - Collabora:  collabora:9980     /hosting/discovery -> XML <wopi-discovery>
#   - OnlyOffice: documentserver:80  /healthcheck       -> "true"
exec_in_nc() {
    docker exec "$ct" /bin/sh -c "$1" 2>/dev/null
}

alive() {
    case "$1" in
        collabora)
            exec_in_nc 'curl -fsS --max-time 5 http://collabora:9980/hosting/discovery 2>/dev/null' \
                | grep -q '<wopi-discovery' ;;
        onlyoffice)
            exec_in_nc 'curl -fsS --max-time 5 http://documentserver/healthcheck 2>/dev/null' \
                | grep -qx 'true' ;;
    esac
}

if alive "$OTHER" && ! alive "$EDITOR"; then
    cat >&2 <<EOF
error: this button wires $NAME, but office.$BASE is serving $OTHER_NAME.

To switch:
  1. Portainer > App Templates > $OTHER > Stop
  2. Portainer > App Templates > $EDITOR > Deploy
  3. Re-click "Wire Nextcloud $NAME" here.

Nextcloud-side state was NOT changed. Safe to dismiss + retry once
the office template is the one you want.
EOF
    exit 3
fi

if ! alive "$EDITOR"; then
    case "$EDITOR" in
        collabora) cat >&2 <<EOF
error: Collabora is not reachable on the catena-network alias collabora:9980.

Check: Portainer > App Templates > collabora > Logs.
       The container should answer GET /hosting/discovery with XML.
       Wait ~30 s after Deploy for coolwsd to start, then retry.
EOF
            ;;
        onlyoffice) cat >&2 <<EOF
error: OnlyOffice is not reachable on the catena-network alias documentserver:80.

Check: Portainer > App Templates > onlyoffice > Logs.
       The container should answer GET /healthcheck with the literal "true".
       Wait ~1 min after Deploy for the document server to boot, then retry.
EOF
            ;;
    esac
    exit 4
fi

if alive "$OTHER"; then
    echo "warning: BOTH $NAME and $OTHER_NAME are running. Traefik may"
    echo "         pick either route for office.$BASE. Stop $OTHER_NAME in"
    echo "         Portainer to avoid the conflict. Continuing with $NAME..."
fi

case "$EDITOR" in
    collabora)  echo "Detected: Collabora at $OFFICE_URL (internal alias collabora:9980)" ;;
    onlyoffice) echo "Detected: OnlyOffice at $OFFICE_URL (internal alias documentserver:80)" ;;
esac

# --- 3. OnlyOffice: read JWT_SECRET from the running documentserver -----
if [ "$EDITOR" = onlyoffice ]; then
    ds=$(docker ps \
        --filter 'label=vps.app=catena-onlyoffice' \
        --filter 'label=vps.component=documentserver' \
        --format '{{.Names}}' | head -n1)

    if [ -z "$ds" ]; then
        echo "error: documentserver container not found despite the healthcheck probe passing." >&2
        echo "       Check Portainer > App Templates > onlyoffice > status." >&2
        exit 5
    fi

    JWT_SECRET=$(get_env "$ds" JWT_SECRET)
    if [ -z "$JWT_SECRET" ]; then
        echo "error: JWT_SECRET is not set in the documentserver container env." >&2
        echo "       Open Portainer > App Templates > onlyoffice > Edit > Environment" >&2
        echo "       and confirm JWT_SECRET is non-empty, then redeploy + retry." >&2
        exit 6
    fi
fi

# --- 4. Remove the other editor's Nextcloud-side state ------------------
OTHER_APP=$(nc_app "$OTHER")
if nc_has_app "$OTHER_APP"; then
    echo "Removing the Nextcloud $OTHER_NAME app ($OTHER_APP)..."
    occ app:remove "$OTHER_APP"
    for k in $(residual_keys "$OTHER"); do
        occ config:app:delete "$OTHER_APP" "$k" >/dev/null 2>&1 || true
    done
    echo "  $OTHER_NAME Nextcloud app removed; residual config cleared."
fi

# --- 5. Install + enable this editor's Nextcloud app --------------------
APP=$(nc_app "$EDITOR")
if nc_has_app "$APP"; then
    echo "$APP app already present; ensuring enabled..."
    occ app:enable "$APP" >/dev/null
else
    echo "Installing $APP app from the Nextcloud appstore..."
    occ app:install "$APP"
fi

# --- 6. Configure the app to point at $OFFICE_URL -----------------------
case "$EDITOR" in
    collabora)
        echo "Configuring richdocuments WOPI URL: $OFFICE_URL"
        occ config:app:set richdocuments wopi_url --value="$OFFICE_URL" >/dev/null
        occ config:app:set richdocuments public_wopi_url --value="$OFFICE_URL" >/dev/null
        # Cloudflare Tunnel terminates the LE cert; coolwsd serves plain HTTP
        # on 9980 and trusts ssl.termination=true. Nextcloud verifies the
        # PUBLIC URL's cert end-to-end, which is the correct posture.
        occ config:app:set richdocuments disable_certificate_verification \
            --value="no" >/dev/null

        # `richdocuments:activate-config` (NC 28+) re-fetches
        # /hosting/discovery from coolwsd and caches the WOPI handshake. On an
        # earlier Nextcloud the next file-open does the same lazily; either
        # path converges.
        occ richdocuments:activate-config \
            || echo "  (activate-config not available on this NC; first file open will refresh discovery)"

        echo
        echo "Collabora wired in Nextcloud."
        echo "  WOPI host:  $OFFICE_URL"
        echo
        echo "Verify: open any DOCX/XLSX/PPTX/ODT file in Nextcloud."
        echo "        It should open in the embedded Collabora editor."
        ;;
    onlyoffice)
        # DocumentServerUrl is the URL the user's BROWSER hits for the editor
        # iframe; DocumentServerInternalUrl is the URL the Nextcloud server-side
        # WOPI client hits for callbacks. They are identical for catena
        # (everything goes through the public CF tunnel), which the OnlyOffice
        # app accepts.
        echo "Configuring onlyoffice DocumentServerUrl: $OFFICE_URL/"
        occ config:app:set onlyoffice DocumentServerUrl --value="$OFFICE_URL/" >/dev/null
        occ config:app:set onlyoffice DocumentServerInternalUrl --value="$OFFICE_URL/" >/dev/null
        # StorageUrl="" makes Nextcloud derive the storage URL from its own
        # overwrite.cli.url (its public URL).
        occ config:app:set onlyoffice StorageUrl --value="" >/dev/null
        # Passed through the exec environment so the secret never appears in
        # this script's argv on the host.
        docker exec --user 33 -e JWT_SECRET="$JWT_SECRET" "$ct" \
            sh -c "php $NC_ROOT/occ config:app:set onlyoffice jwt_secret --value=\"\$JWT_SECRET\"" \
            >/dev/null

        # OnlyOffice has no verify command like richdocuments:activate-config;
        # the next file-open performs the JWT-signed handshake. Re-probe
        # /healthcheck so this run ends on a green light.
        if alive onlyoffice; then
            echo "  documentserver /healthcheck returned 'true' from inside Nextcloud."
        fi

        echo
        echo "OnlyOffice wired in Nextcloud."
        echo "  DocumentServerUrl: $OFFICE_URL/"
        echo "  JWT:               configured (secret read from documentserver container env)"
        echo
        echo "Verify: open any DOCX/XLSX/PPTX file in Nextcloud."
        echo "        It should open in the embedded OnlyOffice editor."
        ;;
esac
