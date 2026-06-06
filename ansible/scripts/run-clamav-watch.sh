#!/bin/sh
# Installed by roles/infrastructure (clamav.yml). systemd-timer-driven
# (catena-clamav-watch.timer). Pings the self-hosted Healthchecks plane
# so a DOWN shared clamd PAGES the operator -- but ONLY when a consumer
# (the mail server's dms container or Nextcloud) is actually up.
#
# Gate rationale: clamd is shared infra with no value of its own. On a
# tenant without mail or Nextcloud -- or while BOTH consumers are
# mid-redeploy -- clamd being unreachable is not an incident, so we
# report SUCCESS (keep the check green) instead of paging. We only ping
# /fail when a consumer is up AND clamd is unreachable. (The other
# fail-loud path, when mail is up, is rspamd's soft-reject on
# CLAM_VIRUS_FAIL; this watch is the page.)
#
# Always exits 0 (diagnostic timer, not control flow). Pings are
# best-effort. Reporting success when no consumer is up also keeps the
# Healthchecks check from going stale (a missed ping would otherwise
# look like an outage).
set -eu

ENV_FILE="${1:-/etc/catena/clamav-watch.env}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -z "${HC_PING_KEY:-}" ]; then
    log "HC_PING_KEY empty (Healthchecks not wired); clamav-watch is a no-op"
    exit 0
fi

ping_hc() {
    # $1 = "" for success, "/fail" for failure. ?create=1 auto-provisions
    # the check on first ping.
    suffix="${1:-}"
    url="http://127.0.0.1:${HEALTHCHECKS_LOOPBACK_PORT}/ping/${HC_PING_KEY}/${CLAMAV_WATCH_SLUG}${suffix}?create=1"
    curl -fsS -m 10 --retry 2 "$url" >/dev/null 2>&1 || true
}

running() {
    # Pass docker ps --filter args; prints the first matching name (empty
    # if none running).
    docker ps "$@" --format '{{.Names}}' 2>/dev/null | head -n1
}

dms_up=$(running --filter 'label=com.docker.compose.service=dms')
nc_up=$(running --filter 'name=nextcloud-' --filter 'label=com.docker.compose.service=app')

if [ -z "$dms_up" ] && [ -z "$nc_up" ]; then
    log "no clamd consumer up (no dms, no nextcloud); reporting OK (no page)"
    ping_hc
    exit 0
fi

clamav_ct=$(running --filter 'label=com.docker.compose.service=clamav')
if [ -z "$clamav_ct" ]; then
    log "consumer up but NO clamav container running; pinging /fail"
    ping_hc /fail
    exit 0
fi

if docker exec "$clamav_ct" clamdcheck.sh >/dev/null 2>&1; then
    log "clamd OK (consumer up); pinging success"
    ping_hc
else
    log "clamd UNREACHABLE (consumer up); pinging /fail"
    ping_hc /fail
fi
exit 0
