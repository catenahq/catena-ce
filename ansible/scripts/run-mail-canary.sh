#!/bin/sh
# Installed by roles/infrastructure (mailserver_canary.yml). systemd-timer
# driven (catena-mail-canary.timer). Proves the mail server is up AND
# actually filtering, via host-side docker-exec into the dms container --
# no external SMTP/IMAP and no auth (the server is OAuth2-only, so there
# is no password to log in with). Pings the self-hosted Healthchecks
# plane; /fail on any check failure.
#
# No-op (success-less exit) when the mailserver is not deployed (no dms
# container), so this never pages on a host without mail.
#
# Checks (both must pass):
#   1. dms is listening on 25 (SMTP) and 993 (IMAPS).
#   2. The rspamd -> clamav antivirus path works: scan the EICAR test
#      pattern through rspamc and assert the antivirus verdict fires.
#      This proves scanning actually happens, not just that a port is
#      open. (External reachability of :25 is covered by validate.yml's
#      registry nmap; disk pressure by the daily disk-preflight.)
#
# Why rspamc (not inject-to-a-mailbox): a maildir canary would need a
# persistent local mailbox, but mailbox_sync reaps any account in a
# managed domain that is not a Keycloak member. rspamc scans through the
# full module chain (incl. the clamav antivirus module) with no mailbox,
# no recipient, no auth.
set -eu

ENV_FILE="${1:-/etc/catena/mail-canary.env}"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

ct=$(docker ps --filter 'label=com.docker.compose.service=dms' \
    --format '{{.Names}}' 2>/dev/null | head -n1)
if [ -z "$ct" ]; then
    log "mailserver not deployed (no dms container); canary is a no-op"
    exit 0
fi

if [ -z "${HC_PING_KEY:-}" ]; then
    log "HC_PING_KEY empty (Healthchecks not wired); canary cannot report"
    exit 0
fi

ping_hc() {
    suffix="${1:-}"
    url="http://127.0.0.1:${HEALTHCHECKS_LOOPBACK_PORT}/ping/${HC_PING_KEY}/${MAIL_CANARY_SLUG}${suffix}?create=1"
    curl -fsS -m 10 --retry 2 "$url" >/dev/null 2>&1 || true
}

fail=""

# 1. Ports listening inside the container.
listen=$(docker exec "$ct" ss -lntH 2>/dev/null || true)
for port in 25 993; do
    if ! printf '%s' "$listen" | grep -q ":${port} "; then
        fail="${fail} port-${port}-not-listening"
    fi
done

# 2. EICAR through rspamc -> the antivirus symbol must fire. The
#    signature is assembled from two halves so the literal test string is
#    never stored verbatim in the repo (it would trip AV scanners on the
#    repo itself). Single-quoted halves: backslash + % + $ are all literal.
eicar_body() {
    printf '%s%s\n' \
        'X5O!P%@AP[4\PZX54(P^)7CC)7}' \
        '$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
}
verdict=$(eicar_body | docker exec -i "$ct" rspamc 2>/dev/null || true)
if ! printf '%s' "$verdict" | grep -q 'CLAM_VIRUS'; then
    fail="${fail} eicar-not-detected"
fi

if [ -z "$fail" ]; then
    log "mail canary OK (ports 25+993 listening, EICAR detected by rspamd->clamav)"
    ping_hc
else
    log "mail canary FAIL:${fail}"
    ping_hc /fail
fi
exit 0
