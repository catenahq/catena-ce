#!/bin/sh
# catena-traefik-nudge.sh
#
# Recover catena-traefik after a docker.service restart.
#
# Background: catena-traefik is a plain container (not a swarm service)
# with --restart=always, wired to the swarm-managed overlay
# `catena-network`. On docker.service restart the overlay network only
# materializes after the swarm manager re-initializes, which races the
# container's own restart loop. Traefik typically fails with:
#
#   failed to set up container networking: could not find a network
#   matching network mode catena-network: network catena-network not
#   found
#
# and then docker gives up retrying (plain containers don't benefit
# from swarm's reconciliation). The web stack is then down until an
# operator manually `docker start catena-traefik`s it.
#
# This script is invoked by catena-traefik-nudge.service on boot and
# on every docker.service restart. It polls until the overlay is
# available, then (if traefik is not already running) starts it.
# Idempotent; exits 0 on success or if traefik is absent (not yet
# deployed).

set -u

NET="${CATENA_NETWORK:-catena-network}"
CTR="${CATENA_TRAEFIK_CTR:-catena-traefik}"
MAX_WAIT="${CATENA_TRAEFIK_NUDGE_TIMEOUT:-60}"

log() { printf '[%s] catena-traefik-nudge: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# If the container doesn't exist yet (e.g. first converge, before the
# traefik role has deployed it), there's nothing to nudge. Exit 0.
if ! docker inspect --type=container "$CTR" >/dev/null 2>&1; then
    log "$CTR not found; nothing to do"
    exit 0
fi

# Wait for the overlay network first, BEFORE checking container state.
# Rationale: docker's own `restart=always` is racing with us; if we
# short-circuit on a transient "Running=true" we miss the case where
# traefik started briefly, failed on network, and is between retries.
# By waiting out the overlay-availability window, we ensure any decision
# we make afterwards is made against steady state.
i=0
while [ "$i" -lt "$MAX_WAIT" ]; do
    if docker network inspect "$NET" >/dev/null 2>&1; then
        log "network $NET available after ${i}s"
        break
    fi
    i=$((i + 1))
    sleep 1
done

if ! docker network inspect "$NET" >/dev/null 2>&1; then
    log "timed out after ${MAX_WAIT}s waiting for network $NET; giving up"
    exit 1
fi

# Give docker's own restart=always a beat to settle after the network
# appeared. 3s is enough for dockerd to notice the network and dispatch
# a restart; shorter makes this script race against dockerd itself.
sleep 3

# Now check: is traefik running?
if [ "$(docker inspect --format='{{.State.Running}}' "$CTR" 2>/dev/null)" = "true" ]; then
    log "$CTR running (docker restart=always handled recovery)"
    exit 0
fi

log "$CTR not running; starting"
if docker start "$CTR" >/dev/null; then
    log "$CTR started"
    exit 0
else
    log "docker start $CTR FAILED"
    exit 1
fi
