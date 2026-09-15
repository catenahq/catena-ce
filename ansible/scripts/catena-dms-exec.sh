#!/bin/bash
# Run one docker command against the mailserver's dms container, resolving the
# container name INSIDE this invocation.
#
# WHY THIS EXISTS. A swarm task name is true for an instant. docker-mailserver
# restarts its way out of the very state the converge repairs -- it polls 120s
# for a mailbox and shuts down without one -- and every restart mints a new
# task id. A name resolved in one Ansible task and interpolated into a later
# one is routinely dead by the time it is used:
#
#     Error response from daemon: No such container:
#         catena-mailserver_dms.1.rg3u7p8hjh2vom6052uoz4322
#
# mailserver_accounts.yml and mailserver_oidc.yml each solved that by opening
# every shell task with the same four-line `ct=$(docker ps ...)` prelude, its
# own retry envelope and its own copy of the explanation -- eight copies of one
# lookup. mailserver_filtering.yml could not: its eight calls are `command:
# argv:` tasks with loops and sha comparisons, so the prelude had no place to
# go, and it interpolated one resolved name into all eight instead. That is the
# defect the prelude exists to prevent, in the one file that could not host it.
#
# So the lookup moves here, once, and an argv task can use it:
#
#     argv: [/usr/local/bin/catena-dms-exec, exec, --, rspamadm, configtest]
#     argv: [/usr/local/bin/catena-dms-exec, cp, /host/file, /in/container]
#
# RETRIED, because landing between restarts is normal here and the next attempt
# gets the next container. FAILS LOUD when none appears: a dms that never comes
# up is a broken host, and exiting 0 would hand the converge a mailserver with
# no filtering and call it done.
set -uo pipefail

ATTEMPTS="${CATENA_DMS_ATTEMPTS:-12}"
DELAY="${CATENA_DMS_DELAY:-5}"

usage() {
  echo "usage: catena-dms-exec exec -- <command...>" >&2
  echo "       catena-dms-exec cp <host-path> <container-path>" >&2
  exit 2
}

resolve() {
  docker ps --filter label=vps.app=catena-mailserver \
    --filter label=vps.component=dms \
    --format '{{.Names}}' 2>/dev/null | head -n1
}

[ "$#" -ge 2 ] || usage
verb="$1"; shift
case "$verb" in
  exec) [ "${1:-}" = "--" ] && shift ;;
  cp)   [ "$#" -eq 2 ] || usage ;;
  *)    usage ;;
esac

attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  ct="$(resolve)"
  if [ -n "$ct" ]; then
    if [ "$verb" = "cp" ]; then
      docker cp "$1" "$ct:$2" && exit 0
    else
      docker exec "$ct" "$@" && exit 0
    fi
    rc=$?
    # A container that vanished between the lookup and the call is the case
    # this retries. Any other non-zero is the command's own verdict and must
    # reach the caller unchanged -- retrying a `rspamadm configtest` that found
    # a broken drop-in would report the breakage as a slow start.
    if [ -n "$(resolve)" ]; then
      exit "$rc"
    fi
  fi
  attempt=$((attempt + 1))
  [ "$attempt" -le "$ATTEMPTS" ] && sleep "$DELAY"
done

echo "catena-dms-exec: no dms container after $ATTEMPTS attempts;" \
     "check 'docker service ps --no-trunc catena-mailserver_dms'" >&2
exit 1
