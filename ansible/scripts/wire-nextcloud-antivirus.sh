#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-antivirus -- point Nextcloud's
# files_antivirus app at the shared clamd (catena-clamav network). Backs
# the "Wire Nextcloud Antivirus" Actions-tab button. Operator clicks once
# after deploying Nextcloud; every occ config:set is an upsert, so it is
# idempotent and safe to re-click after a redeploy.
#
# The shared clamd is deployed by
# catena-ce/ansible/reconcile/roles/infrastructure/tasks/clamav.yml and
# reachable as clamav:3310 on the catena-clamav network, which the Nextcloud
# app + cron services join (catena-templates
# blueprints/nextcloud-s3-oidc/docker-compose.yml).
#
# Failure-mode note: with av_block_unreachable on, an upload files_antivirus
# gets no verdict for is refused ("No connection to anti virus. Upload cannot
# be completed.") and never stored unscanned. A daemon-mode scan waits for
# clamd's answer up to PHP's socket timeout, so an upload made during the
# pause of a blocking signature reload waits for its scan. A down clamd pages
# through the catena-clamav-watch timer.

set -euo pipefail

ct=$(/usr/local/bin/catena-nextcloud-container --required)
echo "Found Nextcloud container: $ct"

# Shared-clamd coordinates. Overridable via env for non-default setups;
# the defaults match clamav.compose.yml.j2 + the catena-clamav network.
CLAMAV_HOST="${CATENA_CLAMAV_HOST:-clamav}"
CLAMAV_PORT="${CATENA_CLAMAV_PORT:-3310}"
# Stream cap must be >= Nextcloud max upload AND the shared clamd
# StreamMaxLength, else large files are skipped rather than scanned.
STREAM_MAX="${CATENA_CLAMAV_STREAM_MAX:-104857600}"
# Upper bound on what files_antivirus will scan (av_max_file_size, -1 =
# no limit upstream). Two reasons to bound it rather than leave -1:
#
#   1. Correctness. -1 promises to scan files larger than STREAM_MAX,
#      which clamd cannot accept -- the scan errors instead of being
#      skipped cleanly. Pinning the two to the same value makes the
#      policy honest: anything we cannot stream, we declare unscanned.
#   2. Upload latency. Upstream's own description of this key is "File
#      size limit for periodic background scans and chunked uploads",
#      so it gates the synchronous scan on the final chunked-upload
#      MOVE. Catena publishes Nextcloud through a Cloudflare Tunnel,
#      and Cloudflare kills any origin request still unanswered after
#      100s with a 524. A multi-GB assemble + full scan inside that one
#      MOVE blows the budget, so the upload fails at 100% after the
#      client has already shipped every byte.
#
# Tradeoff, stated plainly: files ABOVE this size are not scanned on
# upload. That is a real gap; the compensating control is the mail-side
# rspamd fail-loud path.
MAX_SCAN="${CATENA_CLAMAV_MAX_FILE_SIZE:-104857600}"
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
occ config:app:set files_antivirus av_max_file_size --value "$MAX_SCAN"
occ config:app:set files_antivirus av_infected_action --value "$INFECTED_ACTION"
occ config:app:set files_antivirus av_block_unreachable --type boolean --value true

echo
echo "Nextcloud files_antivirus wired to the shared clamd."
echo
echo "Verify: upload the harmless EICAR test file"
echo "  (https://www.eicar.org/download-anti-malware-testfile/)"
echo "and confirm Nextcloud blocks/flags it."
