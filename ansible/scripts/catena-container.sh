#!/bin/bash
# /usr/local/bin/catena-container [--required] <app> <service> -- print the name
# of the running container of one service of a deployed application. Prints
# nothing when it is not running; with --required, says how to deploy it on
# stderr and exits 1 instead. A failed `docker ps` exits 2: "could not look" is
# a different answer from "not running".
#
# <app> is the application's stack name in Portainer (catena-nextcloud) and
# <service> its key in the compose file (app). Swarm labels every task
# container with com.docker.swarm.service.name=<app>_<service>, so this pins
# that one service and not its cron, db or redis peers.

set -euo pipefail

required=""
if [ "${1:-}" = "--required" ]; then
    required=1
    shift
fi
if [ "$#" -ne 2 ]; then
    echo "usage: catena-container [--required] <app> <service>" >&2
    exit 64
fi
app="$1"
service="$2"

if ! out=$(docker ps \
        --filter "label=com.docker.swarm.service.name=${app}_${service}" \
        --filter status=running \
        --format '{{.Names}}'); then
    echo "docker ps failed while resolving the $app $service container" >&2
    exit 2
fi
ct="${out%%$'\n'*}"

if [ -z "$ct" ] && [ -n "$required" ]; then
    echo "The $app application is not running on this host." >&2
    echo >&2
    echo "Deploy it first from Portainer > App Templates, wait for it to come" >&2
    echo "up, then press this button again." >&2
    exit 1
fi

echo "$ct"
