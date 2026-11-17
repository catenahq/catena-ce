#!/bin/bash
# /usr/local/bin/catena-container [--required] <app> <service> -- print the name
# of a locally running container of a desired Swarm task. Prints
# nothing when it is not running; with --required, says how to deploy it on
# stderr and exits 1 instead. A failed Docker query exits 2: "could not look" is
# a different answer from "not running".
#
# <app> is the application's stack name (catena-myapp) and
# <service> its key in the compose file (app). Swarm labels every task
# container with com.docker.swarm.service.name=<app>_<service>, so this pins
# that one service and not its cron, db or redis peers. --service <name>
# addresses a standalone Swarm service by its full name.

set -euo pipefail

required=""
if [ "${1:-}" = "--required" ]; then
    required=1
    shift
fi
if [ "$#" -ne 2 ]; then
    echo "usage: catena-container [--required] <app> <service> | --service <name>" >&2
    exit 64
fi
app="$1"
service="$2"
if [ "$app" = "--service" ]; then
    service_name="$service"
    app="$service"
else
    service_name="${app}_${service}"
fi

if ! out=$(docker ps \
        --filter "label=com.docker.swarm.service.name=$service_name" \
        --filter status=running \
        --format '{{.Names}}'); then
    echo "docker ps failed while resolving the $app $service container" >&2
    exit 2
fi
ct=""
if [ -n "$out" ]; then
    if ! tasks=$(docker service ps "$service_name" --no-trunc \
            --filter desired-state=running --format '{{.Name}}.{{.ID}}'); then
        echo "docker service ps failed while resolving the $service_name container" >&2
        exit 2
    fi
    while IFS= read -r task; do
        [ -n "$task" ] || continue
        while IFS= read -r candidate; do
            if [ "$candidate" = "$task" ]; then
                ct="$candidate"
                break
            fi
        done <<< "$out"
        [ -z "$ct" ] || break
    done <<< "$tasks"
fi

if [ -z "$ct" ] && [ -n "$required" ]; then
    echo "The $app application is not running on this host." >&2
    echo >&2
    echo "Deploy it first from the panel's Apps page, wait for it to come" >&2
    echo "up, then press this button again." >&2
    exit 1
fi

echo "$ct"
