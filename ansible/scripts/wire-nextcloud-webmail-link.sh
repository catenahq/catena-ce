#!/bin/bash
# /usr/local/bin/catena-wire-nextcloud-webmail-link -- add a top-level
# "Webmail" link to the Nextcloud nav that opens the standalone Roundcube
# webmail (mailserver template) in a NEW TAB.
#
# Why redirect (not embed): Roundcube logs in via Keycloak OAuth2, and OAuth
# flows break inside an iframe (X-Frame-Options + third-party-cookie
# blocking). The External Sites app's `redirect` flag navigates top-level
# instead of iframing, so SSO works. This is the whole reason the mailserver
# template ships standalone Roundcube rather than an embedded webmail.
#
# Usage: catena-wire-nextcloud-webmail-link https://webmail.<zone>
#
# Idempotent: enables the `external` app (no-op if already enabled) and sets
# config.php external_sites[0] to the Webmail entry via occ (re-setting the
# same values converges).

set -euo pipefail

WEBMAIL_URL="${1:-${CATENA_WEBMAIL_URL:-}}"
if [ -z "$WEBMAIL_URL" ]; then
    echo "usage: $0 https://webmail.<zone>" >&2
    exit 2
fi

# Match the name prefix AND the compose service label. Two name= filters
# are ORed by docker (so they would also match -cron-/-db-/-redis-); a
# name= plus a label= are different keys and get ANDed, pinning the app
# container exactly.
ct=$(docker ps \
    --filter 'name=nextcloud-' \
    --filter 'label=com.docker.compose.service=app' \
    --format '{{.Names}}' | head -n1)

if [ -z "$ct" ]; then
    echo "Nextcloud is not running on this host; skipping webmail link."
    exit 0
fi

echo "Found Nextcloud container: $ct"
occ() { docker exec --user 33 "$ct" php /var/www/html/occ "$@"; }

# Ensure the External Sites app is present + enabled. The appstore is an
# external dependency: apps.nextcloud.com transiently returns 5xx, and a
# freshly-booted Nextcloud may not have fetched its appstore catalog yet,
# so the first download can fail even when a compatible release exists.
# Retry the download a few times. `occ app:install` exits non-zero BOTH
# when the app is already installed (re-converge: the converged state) AND
# when the download failed, so app:enable -- which only succeeds once the
# app is on disk -- is what tells the two apart and is the load-bearing
# convergence step. (Same reason the earlier app:list grep-guard was
# removed: it aborted under set -e.)
echo "Ensuring the External Sites app is installed + enabled..."
enabled=""
for attempt in 1 2 3 4 5; do
    occ app:install external >/dev/null 2>&1 || true
    if occ app:enable external >/dev/null 2>&1; then
        enabled=1
        break
    fi
    echo "External Sites app not available yet (attempt ${attempt}/5;" \
        "appstore may be unreachable); retrying in 15s..."
    sleep 15
done

# The Webmail nav link is a cosmetic convenience (a top-level shortcut to
# Roundcube). If the External Sites app still cannot be installed after
# retries -- a sustained apps.nextcloud.com outage -- WARN and exit 0
# rather than abort the entire converge over one upstream hiccup. This is
# the same fail-OPEN stance files_antivirus takes: the suite is fully
# functional without the shortcut, and re-running this wiring once the
# appstore is reachable adds the link.
if [ -z "$enabled" ]; then
    echo "WARNING: External Sites app (external) could not be installed from" >&2
    echo "the Nextcloud appstore after 5 attempts; apps.nextcloud.com appears" >&2
    echo "unreachable. Skipping the Webmail nav link -- the suite is otherwise" >&2
    echo "unaffected. Re-run catena-wire-nextcloud-webmail-link once the" >&2
    echo "appstore is reachable to add the shortcut." >&2
    exit 0
fi

# Idempotency guard: only (re)write external_sites[0] when it does not
# already match the desired Webmail entry. Without this the occ
# config:system:set calls + the "Configuring" line below fire on every
# converge, so the Ansible task (changed_when keyed on "Configuring the
# Webmail nav link") reports changed forever and a post-restore
# idempotency rerun never settles. config:system:get prints the stored
# scalar (url -> the URL, name -> Webmail, redirect -> true) or nothing
# when unset, so a three-field compare is a reliable converged signal.
cur_url=$(occ config:system:get external_sites 0 url 2>/dev/null || true)
cur_name=$(occ config:system:get external_sites 0 name 2>/dev/null || true)
cur_redirect=$(occ config:system:get external_sites 0 redirect 2>/dev/null || true)

if [ "$cur_url" = "$WEBMAIL_URL" ] && [ "$cur_name" = "Webmail" ] \
        && [ "$cur_redirect" = "true" ]; then
    echo "Webmail nav link already current -> ${WEBMAIL_URL}; no change."
    exit 0
fi

# Write the Webmail entry as external_sites[0] in config.php. `redirect`
# (boolean true) makes the nav item open the URL top-level / new tab rather
# than in an iframe -- required for Roundcube's Keycloak OAuth login.
echo "Configuring the Webmail nav link -> ${WEBMAIL_URL} (redirect mode)..."
occ config:system:set external_sites 0 id --value 1 --type integer
occ config:system:set external_sites 0 name --value "Webmail"
occ config:system:set external_sites 0 url --value "$WEBMAIL_URL"
occ config:system:set external_sites 0 icon --value "external.svg"
occ config:system:set external_sites 0 type --value "link"
occ config:system:set external_sites 0 lang --value ""
occ config:system:set external_sites 0 device --value ""
occ config:system:set external_sites 0 redirect --value true --type boolean

echo
echo "Done. A 'Webmail' item now appears in the Nextcloud app menu and"
echo "opens ${WEBMAIL_URL} in a new tab (Keycloak SSO, no iframe)."
