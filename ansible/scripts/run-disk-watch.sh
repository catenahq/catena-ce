#!/bin/sh
# Watch filesystem fill level on the paths in DISK_WATCH_PATHS and alert
# BEFORE the disk is full.
#
# Installed by roles/infrastructure (disk_watch.yml), systemd-timer-driven
# (catena-disk-watch.timer). Thresholds:
#
#   >= DISK_WATCH_CRIT_PCT (default 90): ntfy publish at urgent priority
#       AND ping the Healthchecks check /fail (pages via the standard
#       Healthchecks -> ntfy escalation).
#   >= DISK_WATCH_WARN_PCT (default 80): ntfy publish at high priority;
#       the Healthchecks check stays green (warning, not an incident).
#   below warn: success ping only (keeps the check from going stale).
#
# Empty HC_PING_KEY = no Healthchecks pings; empty NTFY_TOPIC = no direct
# ntfy publishes. Both empty = pure journal log. Always exits 0
# (diagnostic timer, not control flow).
#
# DISK_WATCH_FORCE_PCT overrides the measured maximum -- the bench uses
# it to prove the emit path without filling a disk.
set -eu

ENV_FILE="${1:-/etc/catena/disk-watch.env}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

WARN="${DISK_WATCH_WARN_PCT:-80}"
CRIT="${DISK_WATCH_CRIT_PCT:-90}"
PATHS="${DISK_WATCH_PATHS:-/ /mnt/data}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

ping_hc() {
    # $1 = "" for success, "/fail" for failure. ?create=1 auto-provisions
    # the check on first ping.
    [ -n "${HC_PING_KEY:-}" ] || return 0
    suffix="${1:-}"
    body="${2:-}"
    url="http://127.0.0.1:${HEALTHCHECKS_LOOPBACK_PORT:-18000}/ping/${HC_PING_KEY}/${DISK_WATCH_SLUG:-disk-usage}${suffix}?create=1"
    curl -fsS -m 10 --retry 2 --data-raw "$body" "$url" >/dev/null 2>&1 || true
}

publish_ntfy() {
    # $1 = priority, $2 = title, $3 = body
    [ -n "${NTFY_TOPIC:-}" ] || return 0
    curl -fsS -m 10 --retry 2 \
        -H "Priority: $1" \
        -H "Title: $2" \
        -d "$3" \
        "${NTFY_SERVER:-https://ntfy.sh}/${NTFY_TOPIC}" >/dev/null 2>&1 || true
}

report=""
max_pct=0
for p in $PATHS; do
    [ -d "$p" ] || continue
    # -P: POSIX single-line output; use% is field 5.
    pct=$(df -P "$p" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    [ -n "$pct" ] || continue
    report="${report}${p}=${pct}% "
    [ "$pct" -gt "$max_pct" ] && max_pct="$pct"
done

if [ -n "${DISK_WATCH_FORCE_PCT:-}" ]; then
    max_pct="$DISK_WATCH_FORCE_PCT"
    report="${report}(forced=${max_pct}%) "
fi

host=$(hostname)
if [ "$max_pct" -ge "$CRIT" ]; then
    log "disk usage CRITICAL (${max_pct}% >= ${CRIT}%): $report"
    publish_ntfy urgent "Disk CRITICAL on ${host}" \
        "Filesystem usage ${max_pct}% (threshold ${CRIT}%): ${report}"
    ping_hc /fail "disk critical: ${report}"
elif [ "$max_pct" -ge "$WARN" ]; then
    log "disk usage WARNING (${max_pct}% >= ${WARN}%): $report"
    publish_ntfy high "Disk warning on ${host}" \
        "Filesystem usage ${max_pct}% (threshold ${WARN}%): ${report}"
    ping_hc "" "disk warning: ${report}"
else
    log "disk usage OK (max ${max_pct}%): $report"
    ping_hc "" "disk ok: ${report}"
fi
exit 0
