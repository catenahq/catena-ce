#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-mail -- install + enable the
# Nextcloud Mail app inside a deployed Nextcloud container. Backs the
# "Wire Nextcloud Mail" catena-admin action.
#
# Why: the Email Archive feature (catena-email-archive sidecar) reads
# per-user IMAP/CalDAV/CardDAV credentials from Nextcloud Mail's
# oc_mail_accounts table. The Mail app has to be enabled before any
# user can configure an account; this script makes it part of the
# standard wiring sweep so the operator does not need a separate occ
# run after deploying Nextcloud from the Dokploy catalog.
#
# Idempotent: probes `occ app:list` for the mail row and only installs
# when missing on disk; `occ app:enable` is itself idempotent.

set -euo pipefail

# Locate the running Nextcloud app container. Dokploy compose names
# look like nextcloud-<hash>-app-<n>. Match the name prefix AND the
# compose service label: two name= filters are ORed by docker (they
# would also match -cron-/-db-/-redis-), but a name= plus a label= are
# different keys and get ANDed, pinning the app container exactly.
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

occ() { docker exec --user 33 "$ct" php /var/www/html/occ "$@"; }

# `occ app:install` downloads the app when absent and exits non-zero with
# "mail already installed" on a re-converge -- which IS the converged
# state, so tolerate it. The app:enable below is the idempotent step that
# converges enabled-state regardless of where we started. (An app:list
# grep-guard here proved unreliable and aborted under set -e.)
echo "Ensuring Nextcloud Mail app is installed + enabled..."
occ app:install mail >/dev/null 2>&1 || true
occ app:enable mail

echo
echo "✓ Nextcloud Mail app installed and enabled."
echo
echo "Users can now configure their IMAP/SMTP accounts at:"
echo "  Nextcloud -> top-right menu -> 'Mail'"
echo
echo "Once at least one user has configured an account, the"
echo "Email Archive sync will pick up the credentials from"
echo "oc_mail_accounts and start mirroring into Nextcloud Files."
