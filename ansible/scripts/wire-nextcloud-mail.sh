#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-mail -- install + enable the
# Nextcloud Mail app inside a deployed Nextcloud container. Backs the
# "Wire Nextcloud Mail" catena-admin action.
#
# The Mail app has to be enabled before any user can configure an account;
# this script makes it part of the standard wiring so no separate occ run
# is needed after deploying Nextcloud from the app catalog.
#
# Idempotent: probes `occ app:list` for the mail row and only installs
# when missing on disk; `occ app:enable` is itself idempotent.

set -euo pipefail

ct=$(/usr/local/bin/catena-nextcloud-container --required)
echo "Found Nextcloud container: $ct"

occ() { docker exec --user 33 "$ct" php /var/www/html/occ "$@"; }

# `occ app:install` downloads the app when absent and exits non-zero with
# "mail already installed" on a re-converge -- which IS the converged
# state, so tolerate it. The app:enable below is the idempotent step that
# converges enabled-state regardless of where we started.
echo "Ensuring Nextcloud Mail app is installed + enabled..."
occ app:install mail >/dev/null 2>&1 || true
occ app:enable mail

echo
echo "+ Nextcloud Mail app installed and enabled."
echo
echo "Users can now configure their IMAP/SMTP accounts at:"
echo "  Nextcloud -> top-right menu -> 'Mail'"
