#!/bin/sh
# Say when this host needs a reboot. Never reboot it.
#
# Debian's own unattended-upgrades applies the security patches on this box
# (bootstrap/roles/common configures it, with Automatic-Reboot OFF), so a
# kernel, libc or systemd update lands and then waits for a boot that, on a
# server nobody logs into, can be never. Applying patches and never saying they
# are not in effect yet is worse than not applying them: the host reports
# itself patched and is running the old code.
#
# Rebooting unattended is not the answer either. The reboot is a minute of
# downtime for every application on the box, and choosing when belongs to
# whoever answers for that. So this reports, and a person decides.
#
# Installed by reconcile/roles/host_maintenance, driven by
# catena-reboot-check.timer.
#
# WHERE IT REPORTS. The Healthchecks plane, the same one the shared-clamd watch
# and the mail canary use: /fail while a reboot is pending, success while it is
# not. Healthchecks notifies on the TRANSITION and escalates to ntfy, so the
# person is told once when the host starts needing a reboot and once when it
# stops -- not nightly for as long as it is pending. That is also why this
# pings on EVERY run rather than only when the answer changes: a check that
# stops hearing from a host goes late, and "late" is how a probe that died
# becomes visible instead of silently reporting nothing forever.
#
# WHAT COUNTS AS NEEDING ONE. /var/run/reboot-required, which the Debian
# packages themselves create, plus needrestart's view of services still running
# on deleted libraries. The second is reported and does NOT page: a service on
# an old library is restarted by the next deploy of that service, while a
# kernel is only replaced by a boot.
#
# Exits 0 on every path it can report on -- a diagnostic timer, not control
# flow. The one non-zero exit is an unwired HC_PING_KEY, where the exit code is
# the only channel left, exactly as run-clamav-watch.sh documents.
set -eu

ENV_FILE="${1:-/etc/catena/reboot-check.env}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -z "${HC_PING_KEY:-}" ]; then
    log "HC_PING_KEY empty in $ENV_FILE: no check can be provisioned, so this"
    log "probe cannot report and its absence cannot be noticed. Re-run the"
    log "converge to re-render the env file from the on-box config store."
    exit 1
fi

ping_hc() {
    suffix="${1:-}"
    url="http://127.0.0.1:${HEALTHCHECKS_LOOPBACK_PORT}/ping/${HC_PING_KEY}/${REBOOT_CHECK_SLUG}${suffix}?create=1"
    curl -fsS -m 10 --retry 2 "$url" >/dev/null 2>&1 || true
}

REPORT_FILE="${REBOOT_CHECK_REPORT_FILE:-/var/lib/catena/reboot-required.json}"

# The packages that asked for the reboot, one per line, as Debian wrote them.
pkgs=""
required="false"
if [ -f /var/run/reboot-required ]; then
    required="true"
    if [ -r /var/run/reboot-required.pkgs ]; then
        pkgs=$(sort -u /var/run/reboot-required.pkgs 2>/dev/null || true)
    fi
fi

# Services running on libraries that have been replaced under them. Absent
# needrestart is not a failure: the kernel half above is the one that pages,
# and this half is detail.
services=""
if command -v needrestart >/dev/null 2>&1; then
    services=$(needrestart -b 2>/dev/null |
        sed -n 's/^NEEDRESTART-SVC: //p' | sort -u || true)
fi

json_array() {
    # Lines on stdin -> a JSON array. No jq dependency: this runs on a host
    # mid-patch-cycle and the fewer things it needs the better.
    printf '['
    first=1
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        [ "$first" = 1 ] || printf ','
        first=0
        printf '"%s"' "$(printf '%s' "$line" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    done
    printf ']'
}

mkdir -p "$(dirname "$REPORT_FILE")"
{
    printf '{\n'
    printf '  "required": %s,\n' "$required"
    printf '  "packages": %s,\n' "$(printf '%s\n' "$pkgs" | json_array)"
    printf '  "services": %s,\n' "$(printf '%s\n' "$services" | json_array)"
    printf '  "kernel_running": "%s",\n' "$(uname -r)"
    printf '  "checked_at": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '}\n'
} > "$REPORT_FILE.tmp"
mv "$REPORT_FILE.tmp" "$REPORT_FILE"

if [ "$required" = "true" ]; then
    log "reboot REQUIRED (packages: $(printf '%s' "$pkgs" | tr '\n' ' '))"
    ping_hc /fail
else
    log "no reboot required (kernel $(uname -r))"
    ping_hc
fi

if [ -n "$services" ]; then
    log "services running on replaced libraries: $(printf '%s' "$services" | tr '\n' ' ')"
fi
exit 0
