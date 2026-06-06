#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-talk-hpb -- post-deploy wiring
# for Nextcloud Talk's High-Performance Backend (HPB).
#
# Backs the "Wire Nextcloud Talk + HPB" catena-admin action. Idempotent:
# every `occ talk:*:add` is an upsert keyed on the URL/host. Re-clicking
# the button after a config change picks up the new values.
#
# Auto-detect: the HPB block in nextcloud-s3.compose.yml may be
# commented out (operator opted to disable HPB). Probe whether the
# `signaling` service is reachable on dokploy-network; if not, log
# and exit 0 -- this script is safe to wire into a single catena-admin
# action that fires unconditionally.
#
# Defaults:
#   turn.<base>:5349  TURN/TLS (TCP+UDP) -- shared coturn
#   stun.<base>:3478  STUN (UDP)         -- shared coturn (alias of TURN host)
#   signaling.<base>  WSS signaling endpoint -- this template's signaling svc

set -euo pipefail

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
    exit 1
fi

echo "Found Nextcloud container: $ct"

# Read env from inside the container so secrets do not travel the
# host argv. Same pattern as wire-nextcloud-oidc.sh.
get_env() {
    docker exec "$ct" /bin/sh -c "printenv \"$1\"" 2>/dev/null || true
}

NC_HOSTNAME=$(get_env NEXTCLOUD_HOSTNAME)
SIGNALING_SECRET=$(get_env SIGNALING_SECRET)
TURN_SECRET=$(get_env TURN_STATIC_AUTH_SECRET)

# Derive the base zone from the Nextcloud hostname (drop the leading
# nextcloud.). Falls back to the full hostname if no leading label.
TURN_HOST=$(echo "$NC_HOSTNAME" | sed -e 's/^nextcloud\.//')
if [ -z "$TURN_HOST" ] || [ "$TURN_HOST" = "$NC_HOSTNAME" ]; then
    TURN_HOST="$NC_HOSTNAME"
fi
TURN_HOSTNAME="turn.$TURN_HOST"
STUN_HOSTNAME="$TURN_HOSTNAME" # coturn STUN + TURN share the host
SIGNALING_HOSTNAME="signaling.$NC_HOSTNAME"

# Auto-detect: is the signaling service alive on dokploy-network? The
# Nextcloud container is on dokploy-network so a short curl from inside
# it is the cheapest probe. aio-talk's signaling layer listens on
# port 8081 inside the container; the dokploy-network alias `signaling`
# points at the talk-hpb service (set in nextcloud-s3.compose.yml).
if ! docker exec "$ct" /bin/sh -c \
        "curl -fsS --max-time 3 http://signaling:8081/api/v1/welcome >/dev/null 2>&1"; then
    echo
    echo "HPB signaling service not reachable from Nextcloud."
    echo "If the talk-hpb service in nextcloud-s3.compose.yml is commented"
    echo "out this is expected -- skipping wiring (Talk runs in built-in"
    echo "P2P mode; small calls work, large calls degrade)."
    echo
    echo "If talk-hpb IS uncommented, diagnose with:"
    echo "  docker ps --filter name=talk-hpb"
    echo "  docker logs --tail 50 \$(docker ps -q --filter name=talk-hpb)"
    exit 0
fi

missing=()
[ -z "$NC_HOSTNAME" ]       && missing+=("NEXTCLOUD_HOSTNAME")
[ -z "$SIGNALING_SECRET" ]  && missing+=("SIGNALING_SECRET")
[ -z "$TURN_SECRET" ]       && missing+=("TURN_STATIC_AUTH_SECRET")

if [ "${#missing[@]}" -gt 0 ]; then
    echo "error: missing required env on $ct:" >&2
    for m in "${missing[@]}"; do echo "  - $m" >&2; done
    echo >&2
    echo "Open Dokploy UI > Templates > nextcloud-s3 > Edit > Environment" >&2
    echo "and confirm the HPB env vars are set, then redeploy." >&2
    exit 2
fi

echo "Wiring Talk + HPB:"
echo "  signaling:    https://$SIGNALING_HOSTNAME"
echo "  TURN host:    $TURN_HOSTNAME (UDP+TCP 5349)"
echo "  STUN host:    $STUN_HOSTNAME:3478"
echo

run_occ() {
    docker exec --user 33 "$ct" php /var/www/html/occ "$@"
}

# Idempotent upserts. `occ talk:turn:add` returns rc=0 even if the
# entry already exists; the entry is deduplicated by host+port+protocol.
run_occ talk:turn:add "$TURN_HOSTNAME:5349" "udp,tcp" --secret="$TURN_SECRET"

# `occ talk:stun:add` similarly upserts by host:port.
run_occ talk:stun:add "$STUN_HOSTNAME:3478"

# Signaling server is keyed on URL; re-adding with the same URL but a
# new secret rotates the secret in place.
run_occ talk:signaling:add "https://$SIGNALING_HOSTNAME" "$SIGNALING_SECRET"

# Force HPB only -- without this, Talk falls back to its built-in mesh
# signaling for some call types, which silently breaks the SFU path.
run_occ config:app:set spreed external_signaling_only --value="yes"

echo
echo "+ Talk + HPB wired."
echo
echo "Verify: open Talk in Nextcloud, start a call, check"
echo "chrome://webrtc-internals -- ICE state should be 'connected'"
echo "and at least one candidate of type 'relay' or 'host' should be"
echo "present. With two participants on different networks the relayed"
echo "path should activate when one is behind a restrictive firewall."
