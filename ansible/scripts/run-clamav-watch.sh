#!/bin/sh
# Page when the shared clamd is down, but only while something depends on
# it -- the mail server's dms container or Nextcloud. Pings the self-hosted
# Healthchecks plane.
#
# Installed by reconcile/roles/infrastructure (clamav.yml), systemd-timer-driven
# (catena-clamav-watch.timer).
#
# Gate rationale: clamd is shared infra with no value of its own. On a
# tenant without mail or Nextcloud -- or while BOTH consumers are
# mid-redeploy -- clamd being unreachable is not an incident, so the watch
# reports SUCCESS (keeps the check green) instead of paging. It pings
# /fail only when a consumer is up AND clamd is unreachable. (The other
# fail-loud path, when mail is up, is rspamd's soft-reject on
# CLAM_VIRUS_FAIL; this watch is the page.)
#
# Exits 0 on every path it can REPORT on (diagnostic timer, not control
# flow); pings are best-effort. Reporting success when no consumer is up
# also keeps the Healthchecks check from going stale (a missed ping would
# otherwise look like an outage). The one non-zero exit is an unwired
# HC_PING_KEY, where the exit code is the only channel left -- see below.
set -eu

ENV_FILE="${1:-/etc/catena/clamav-watch.env}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# An empty ping key is NOT a supported configuration, and the exit code is the
# only channel left when it is empty: the check is auto-provisioned by the
# first ping (?create=1 below), so with no key no check exists, nothing can go
# late, and this watch reports nothing forever while Healthchecks shows a clean
# board. healthchecks_ping_key is INTERNAL_SECRETS -- minted on the box
# on every converge -- so empty means the on-box store did not load when
# clamav-watch.env was rendered. Fail the unit so `systemctl --failed` says so.
if [ -z "${HC_PING_KEY:-}" ]; then
    log "HC_PING_KEY empty in $ENV_FILE: no check can be provisioned, so this"
    log "watch cannot report and its absence cannot be noticed. Re-run the"
    log "converge to re-render the env file from the on-box config store."
    exit 1
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

dms_up=$(running --filter 'label=vps.component=dms')
nc_up=$(running --filter 'label=vps.app=catena-nextcloud' \
                --filter 'label=vps.component=app')

if [ -z "$dms_up" ] && [ -z "$nc_up" ]; then
    log "no clamd consumer up (no dms, no nextcloud); reporting OK (no page)"
    ping_hc
    exit 0
fi

clamav_ct=$(running --filter 'label=vps.component=clamav')
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
