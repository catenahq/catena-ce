#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-antivirus -- point Nextcloud's
# files_antivirus app at the shared clamd (catena-clamav network). Backs
# the "Wire Nextcloud Antivirus" Actions-tab button. Operator clicks once
# after deploying Nextcloud; every occ config:set is an upsert, so it is
# idempotent and safe to re-click after a redeploy.
#
# The shared clamd is deployed by roles/infrastructure clamav.yml and
# reachable as clamav:3310 on the catena-clamav network, which the
# Nextcloud app + cron services join (see nextcloud-s3.compose.yml).
#
# Failure-mode note: files_antivirus fails OPEN -- if clamd is
# unreachable it accepts files unscanned and logs an error. There is no
# clean fail-closed mode (and forcing one floods the log), so we do not
# attempt it. clamd reachability is alerted by the Gatus probe on
# clamav:3310; the fail-loud path is on the mail side (rspamd
# force_actions soft-reject on CLAM_VIRUS_FAIL).

set -euo pipefail

# Match the name prefix AND the compose service label. Two name= filters
# are ORed by docker (so they would also match -cron-/-db-/-redis-); a
# name= plus a label= are different keys and get ANDed, pinning the app
# container exactly.
ct=$(docker ps \
    --filter 'name=nextcloud-' \
    --filter 'label=com.docker.compose.service=app' \
    --format '{{.Names}}' | head -n1)

if [ -z "$ct" ]; then
    echo "Nextcloud is not running on this host."
    echo
    echo "Deploy first: Dokploy UI -> Templates -> nextcloud-s3 -> Deploy."
    echo "Wait for the container to come up, then click this button again."
    exit 1
fi

echo "Found Nextcloud container: $ct"

# Shared-clamd coordinates. Overridable via env for non-default setups;
# the defaults match clamav.compose.yml.j2 + the catena-clamav network.
CLAMAV_HOST="${CATENA_CLAMAV_HOST:-clamav}"
CLAMAV_PORT="${CATENA_CLAMAV_PORT:-3310}"
# Stream cap must be >= Nextcloud max upload AND the shared clamd
# StreamMaxLength, else large files are skipped rather than scanned.
STREAM_MAX="${CATENA_CLAMAV_STREAM_MAX:-104857600}"
# only_log keeps the file but records the detection; delete removes it.
INFECTED_ACTION="${CATENA_CLAMAV_INFECTED_ACTION:-only_log}"

occ() { docker exec --user 33 "$ct" php /var/www/html/occ "$@"; }

echo "Wiring files_antivirus -> ${CLAMAV_HOST}:${CLAMAV_PORT} (daemon mode)..."

# Install from the appstore if absent (NC 25+); no-op if already present.
occ app:install files_antivirus >/dev/null 2>&1 || true
occ app:enable files_antivirus >/dev/null

occ config:app:set files_antivirus av_mode --value daemon
occ config:app:set files_antivirus av_host --value "$CLAMAV_HOST"
occ config:app:set files_antivirus av_port --value "$CLAMAV_PORT"
occ config:app:set files_antivirus av_stream_max_length --value "$STREAM_MAX"
occ config:app:set files_antivirus av_infected_action --value "$INFECTED_ACTION"

echo
echo "Nextcloud files_antivirus wired to the shared clamd."
echo
echo "Verify: upload the harmless EICAR test file"
echo "  (https://www.eicar.org/download-anti-malware-testfile/)"
echo "and confirm Nextcloud blocks/flags it."
