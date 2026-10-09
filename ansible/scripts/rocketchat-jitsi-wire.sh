#!/bin/bash
# /usr/local/bin/catena-wire-rocketchat-jitsi -- post-deploy wiring
# for Rocket.Chat's bundled on-server Jitsi.
#
# Backs the "Wire Rocket.Chat Jitsi" catena-admin action. Idempotent: each
# REST `POST /api/v1/settings/<id>` is upsert-shaped (RC's setting API
# overwrites in place). Re-clicking the button after a domain or
# secret change picks up the new values.
#
# Reads ROOT_URL, the bootstrap admin credentials (ADMIN_USERNAME,
# ADMIN_PASS) and JITSI_HOSTNAME from the rocketchat container's
# environment. The template builds JITSI_HOSTNAME from the same stack
# variable as jitsi-web's PUBLIC_URL, so the Jitsi host Rocket.Chat dials
# is the one jitsi-web answers as. The admin password is the host's
# admin_password, which the catalog sets in the stack environment as
# ADMIN_PASS at deploy time.

set -euo pipefail

ct=$(/usr/local/bin/catena-container --required catena-rocketchat rocketchat)

echo "Found Rocket.Chat container: $ct"

get_env() {
    docker exec "$ct" /bin/sh -c "printenv \"$1\"" 2>/dev/null || true
}

ROOT_URL=$(get_env ROOT_URL)
ADMIN_USER=$(get_env ADMIN_USERNAME)
ADMIN_PASS=$(get_env ADMIN_PASS)
JITSI_DOMAIN=$(get_env JITSI_HOSTNAME)

missing=()
[ -z "$ROOT_URL" ]     && missing+=("ROOT_URL")
[ -z "$ADMIN_USER" ]   && missing+=("ADMIN_USERNAME")
[ -z "$ADMIN_PASS" ]   && missing+=("ADMIN_PASS")
[ -z "$JITSI_DOMAIN" ] && missing+=("JITSI_HOSTNAME")

if [ "${#missing[@]}" -gt 0 ]; then
    echo "error: missing required env on $ct:" >&2
    for m in "${missing[@]}"; do echo "  - $m" >&2; done
    echo >&2
    echo "The stack sets these on its rocketchat service. In Portainer, open" >&2
    echo "Stacks > catena-rocketchat, check them in the editor and in the" >&2
    echo "stack's environment variables, then update the stack." >&2
    exit 2
fi

# Advisory, NOT fatal -- deliberately different from
# nextcloud-talk-hpb-wire.sh, which refuses. Jitsi's primary media path is
# direct JVB UDP on 10000, and coturn is only the restrictive-network
# fallback (JVB_TURN_HOST in the rocketchat compose). So calls work for most
# participants without it; what is lost is the relay for anyone whose network
# blocks UDP 10000. Refusing would withhold a working feature over a
# degraded edge case.
#
# coturn is consumer-gated (reconcile/roles/coturn/tasks/main.yml) and its consumer is
# THIS deployment, so on a freshly deployed host the relay does not come up
# until the next converge. Say so plainly rather than let the fallback be
# silently missing.
if [ -z "$(docker service ls --filter name=coturn --format '{{.Name}}')" ]; then
    echo "Note: the fallback call relay is not running on this server yet."
    echo "Calls will work for participants who can reach the server"
    echo "directly. Anyone on a restrictive network will not connect until"
    echo "the relay is set up, which happens automatically on the next"
    echo "managed operation. Wiring continues."
    echo
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

# The token and user id are read out of the reply with sed, on the host.
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
# Calls open at https://<JITSI_DOMAIN>/<room>: TLS for every public host
# ends at the Cloudflare edge.
set_setting Jitsi_SSL                '{"value":true}'
# Rooms carry no JWT: a call is joined by its link. Every channel can
# start one.
set_setting Jitsi_Enable_Channels    '{"value":true}'

echo
echo "+ Rocket.Chat -> Jitsi wired."
echo
echo "Verify: open a channel in Rocket.Chat, click the phone icon to"
echo "start a video call. The popup loads https://$JITSI_DOMAIN/<room>."
echo "Two participants on different networks should connect via direct"
echo "JVB UDP (port 10000); restrictive-network clients fall back to"
echo "the shared coturn relay on port 5349."
