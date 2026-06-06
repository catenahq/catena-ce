#!/bin/bash
# /usr/local/bin/catena-wire-rocketchat-jitsi -- post-deploy wiring
# for Rocket.Chat's bundled on-server Jitsi.
#
# Backs the "Wire Rocket.Chat Jitsi" catena-admin action. Idempotent: each
# REST `POST /api/v1/settings/<id>` is upsert-shaped (RC's setting API
# overwrites in place). Re-clicking the button after a domain or
# secret change picks up the new values.
#
# Reads the bootstrap admin credentials from the rocketchat container
# environment (ROOT_URL + the OVERWRITE_SETTING_* values shipped in
# the compose). The admin password lives in the vault and is injected
# into the container at deploy time as ROCKETCHAT_ADMIN_PASSWORD.

set -euo pipefail

ct=$(docker ps \
    --filter 'name=rocketchat-' \
    --filter 'name=-rocketchat-' \
    --format '{{.Names}}' | head -n1)

if [ -z "$ct" ]; then
    echo "Rocket.Chat is not running on this host."
    echo
    echo "Deploy first: Dokploy UI > Templates > rocketchat > Deploy."
    exit 1
fi

echo "Found Rocket.Chat container: $ct"

get_env() {
    docker exec "$ct" /bin/sh -c "printenv \"$1\"" 2>/dev/null || true
}

ROOT_URL=$(get_env ROOT_URL)
RC_HOSTNAME=$(echo "$ROOT_URL" | sed -e 's,^https\?://,,' -e 's,/.*,,')
ADMIN_USER=$(get_env ADMIN_USERNAME)
ADMIN_PASS=$(get_env ADMIN_PASS)

# Derive the base zone from the RC hostname (drop the leading
# rocketchat.). Same shape as nextcloud-talk-hpb-wire.sh.
RC_HOSTNAME_BASE=$(echo "$RC_HOSTNAME" | sed -e 's/^rocketchat\.//')
if [ -z "$RC_HOSTNAME_BASE" ] || [ "$RC_HOSTNAME_BASE" = "$RC_HOSTNAME" ]; then
    RC_HOSTNAME_BASE="$RC_HOSTNAME"
fi
JITSI_DOMAIN="meet.$RC_HOSTNAME_BASE"

missing=()
[ -z "$ROOT_URL" ]    && missing+=("ROOT_URL")
[ -z "$ADMIN_USER" ]  && missing+=("ADMIN_USERNAME")
[ -z "$ADMIN_PASS" ]  && missing+=("ADMIN_PASS")

if [ "${#missing[@]}" -gt 0 ]; then
    echo "error: missing required env on $ct:" >&2
    for m in "${missing[@]}"; do echo "  - $m" >&2; done
    echo >&2
    echo "Open Dokploy UI > Templates > rocketchat > Edit > Environment" >&2
    echo "and confirm the bootstrap admin env is set, then redeploy." >&2
    exit 2
fi

echo "Wiring Rocket.Chat -> Jitsi:"
echo "  RC URL:        $ROOT_URL"
echo "  Jitsi domain:  $JITSI_DOMAIN"
echo

# Login to RC's REST API. RC returns {data: {authToken, userId}} on
# success.
login_json=$(docker exec "$ct" /bin/sh -c "
    curl -fsS -X POST \"$ROOT_URL/api/v1/login\" \
        -H 'Content-Type: application/json' \
        -d '{\"user\":\"'\"$ADMIN_USER\"'\",\"password\":\"'\"$ADMIN_PASS\"'\"}'
")

# Parse with the python interpreter inside RC's image (Node, not
# Python -- use jq if available, else hand-parse with sed).
auth_token=$(echo "$login_json" \
    | sed -nE 's/.*"authToken"\s*:\s*"([^"]+)".*/\1/p' \
    | head -n1)
user_id=$(echo "$login_json" \
    | sed -nE 's/.*"userId"\s*:\s*"([^"]+)".*/\1/p' \
    | head -n1)

if [ -z "$auth_token" ] || [ -z "$user_id" ]; then
    echo "error: failed to authenticate against $ROOT_URL/api/v1/login" >&2
    echo "response was: $login_json" >&2
    exit 3
fi

set_setting() {
    local key="$1"
    local value="$2"
    # RC's settings API takes JSON body {"value": <value>}. Numeric
    # / boolean values are passed unquoted; strings get JSON-quoted.
    docker exec "$ct" /bin/sh -c "
        curl -fsS -X POST \"$ROOT_URL/api/v1/settings/$key\" \
            -H 'X-Auth-Token: $auth_token' \
            -H 'X-User-Id: $user_id' \
            -H 'Content-Type: application/json' \
            -d '$value' >/dev/null
    " || {
        echo "warn: failed to set $key" >&2
        return 1
    }
}

# Apply the Jitsi conferencing settings. Each is upsert-shaped:
# RC overwrites the existing setting record in place.
set_setting Jitsi_Enabled            '{"value":true}'
set_setting Jitsi_Domain             "{\"value\":\"$JITSI_DOMAIN\"}"
set_setting Jitsi_URL_Room_Prefix    '{"value":"Catena"}'
set_setting Jitsi_URL_Room_Hash      '{"value":false}'
set_setting Jitsi_SSL                '{"value":true}'
# v1 ships without JWT-gated rooms -- channel access is by RC link.
# Flip Jitsi_Enable_Channels=true to allow conf creation per channel.
set_setting Jitsi_Enable_Channels    '{"value":true}'
# meet.<base> rides cloudflared (proxied: true), which terminates TLS
# at the edge -- the iframe URL is `https://<JITSI_DOMAIN>/<room>`.

echo
echo "+ Rocket.Chat -> Jitsi wired."
echo
echo "Verify: open a channel in Rocket.Chat, click the phone icon to"
echo "start a video call. The popup loads https://$JITSI_DOMAIN/<room>."
echo "Two participants on different networks should connect via direct"
echo "JVB UDP (port 10000); restrictive-network clients fall back to"
echo "the shared coturn relay at turn.$RC_HOSTNAME_BASE:5349."
