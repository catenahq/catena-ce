#!/bin/sh
# catena-network-nudge.sh
#
# Recover containers stranded by the catena-network overlay race after a
# docker.service start (boot, daemon restart, snapshot restore).
#
# Background: `catena-network` is a swarm-scoped overlay. On docker.service
# start, dockerd restores containers that carry a restart policy during its
# "Loading containers" phase, which runs BEFORE the swarm node re-initializes
# and re-materializes the overlay. Any container whose start lands in that
# window fails with:
#
#   failed to set up container networking: could not find a network
#   matching network mode catena-network: network catena-network not found
#
# and docker does NOT retry it: the boot-time restore gets exactly one
# attempt per container, so the container stays Exited with RestartCount=0
# until something starts it. Swarm SERVICES reconcile themselves (the task
# manager re-dispatches by name), but plain/compose containers -- traefik
# and everything the Portainer stacks deploy (keycloak-server-1,
# nextcloud-talk-hpb-1, oauth2-proxy, gatus, ...) -- have nothing to heal
# them. Observed live: a VM boot where the overlay appeared 2.5s after
# "Loading containers: done" left keycloak-server-1 and nextcloud-talk-hpb-1
# dead, while every container started after the overlay appeared joined fine.
#
# This script is invoked by catena-network-nudge.service on boot and on
# every docker.service restart. It polls until the overlay is available,
# then starts what the race stranded.
#
# Two deliberately different rules, because the two cases carry different
# evidence:
#
#   traefik  -- started whenever it is not running. It is the ingress for
#               every public route, it is always meant to be up, and it may
#               be mid-restart-loop rather than carrying a recorded error.
#   others   -- started ONLY when docker recorded the overlay-not-found
#               error on the last start attempt AND the restart policy says
#               docker meant to keep it running. A container the operator
#               stopped on purpose carries no such error and is never
#               touched. Swarm task containers are skipped: swarm owns their
#               lifecycle and re-dispatches new tasks itself, so starting a
#               stale task container here would fight the task manager.
#
# Idempotent; exits 0 when there is nothing to do.

set -u

NET="${CATENA_NETWORK:-catena-network}"
CTR="${CATENA_TRAEFIK_CTR:-catena-traefik}"
MAX_WAIT="${CATENA_NETWORK_NUDGE_TIMEOUT:-60}"

log() { printf '[%s] catena-network-nudge: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Wait for the overlay network first, BEFORE inspecting any container state.
# Rationale: docker's own restart policy is racing with us; if we
# short-circuit on a transient "Running=true" we miss the case where a
# container started briefly, failed on network, and is between retries. By
# waiting out the overlay-availability window, every decision below is made
# against steady state.
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

# Give docker's own restart policy a beat to settle after the network
# appeared. 3s is enough for dockerd to notice the network and dispatch a
# restart; shorter makes this script race against dockerd itself.
sleep 3

rc=0

# ── traefik: the ingress, started whenever it is not running ──────────
if docker inspect --type=container "$CTR" >/dev/null 2>&1; then
    if [ "$(docker inspect --format='{{.State.Running}}' "$CTR" 2>/dev/null)" = "true" ]; then
        log "$CTR running (docker restart policy handled recovery)"
    else
        log "$CTR not running; starting"
        if docker start "$CTR" >/dev/null 2>&1; then
            log "$CTR started"
        else
            log "docker start $CTR FAILED"
            rc=1
        fi
    fi
else
    log "$CTR not found; nothing to do"
fi

# ── everything else stranded by the same race ─────────────────────────
for name in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    [ "$name" = "$CTR" ] && continue

    [ "$(docker inspect --format='{{.State.Running}}' "$name" 2>/dev/null)" = "true" ] && continue

    # Swarm owns its task containers; it re-dispatches replacements itself.
    task_id=$(docker inspect \
        --format='{{index .Config.Labels "com.docker.swarm.task.id"}}' \
        "$name" 2>/dev/null)
    [ -n "$task_id" ] && continue

    # Only what docker meant to keep running.
    policy=$(docker inspect --format='{{.HostConfig.RestartPolicy.Name}}' "$name" 2>/dev/null)
    case "$policy" in
        always | unless-stopped) ;;
        *) continue ;;
    esac

    # The evidence: docker recorded the overlay-not-found failure on the
    # last start attempt. Anything else (clean stop, app crash, OOM) is not
    # this race and is left alone.
    err=$(docker inspect --format='{{.State.Error}}' "$name" 2>/dev/null)
    case "$err" in
        *"network $NET not found"*) ;;
        *) continue ;;
    esac

    log "$name stranded by the $NET race; starting"
    if docker start "$name" >/dev/null 2>&1; then
        log "$name started"
    else
        log "docker start $name FAILED"
        rc=1
    fi
done

exit "$rc"
