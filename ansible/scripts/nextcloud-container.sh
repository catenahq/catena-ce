#!/bin/bash
# /usr/local/bin/catena-nextcloud-container -- print the name of the running
# Nextcloud app container, for the catena-wire-nextcloud-* scripts. Prints
# nothing when Nextcloud is not deployed; with --required, says how to deploy
# it on stderr and exits 1 instead. A failed `docker ps` exits 2: "could not
# look" is a different answer from "not deployed".
#
# Two labels the catalog puts on every service: vps.app names the
# application, vps.component names the service inside it, and docker ANDs
# filters on different keys, so this pins the app container and not its
# cron, db or redis peers. Neither label changes when a client renames the
# stack, which the container NAME does.

set -euo pipefail

if ! out=$(docker ps \
        --filter 'label=vps.app=catena-nextcloud' \
        --filter 'label=vps.component=app' \
        --format '{{.Names}}'); then
    echo "docker ps failed while resolving the Nextcloud container" >&2
    exit 2
fi
ct=$(printf '%s\n' "$out" | head -n1)

if [ -z "$ct" ] && [ "${1:-}" = "--required" ]; then
    echo "Nextcloud is not running on this host." >&2
    echo >&2
    echo "Deploy first: Portainer > App Templates > nextcloud-s3 > Deploy." >&2
    echo "Wait for the container to come up, then click this button again." >&2
    exit 1
fi

echo "$ct"
