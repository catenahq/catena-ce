#!/bin/sh
# catena-restic-env -- single canonical entrypoint for running a
# command with /etc/catena/backup.env loaded into the environment.
#
# Why this exists:
#   backup.env carries RESTIC_REPOSITORY + RESTIC_PASSWORD_FILE +
#   AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY. Production scripts
#   launched by systemd inherit those keys via the unit's
#   EnvironmentFile= directive, so a bare `. /etc/catena/backup.env`
#   inside such a script happens to work even though it never re-exports
#   anything. Callers from OUTSIDE that systemd context (Ansible shell
#   tasks, sudo-over-ssh, ad-hoc operator commands) get a scrubbed env
#   from sudo and must auto-export every key with `set -a` -- and at
#   least one caller (the test bench's auto_update_rollback scenario)
#   forgot, hand-picking the wrong subset and producing a misleading
#   "no-snapshot-found" error when restic could not authenticate to S3.
#
#   This helper hides the auto-export so callers do not have to know
#   which context they are in. A unit test
#   (tests/unit/test_restic_env_pattern.py) enforces that all new
#   callers go through here rather than re-rolling the source pattern.
#
# Usage:
#   sudo catena-restic-env restic snapshots --latest 5
#   sudo catena-restic-env restic cat config
#   sudo catena-restic-env sh -c 'restic snapshots --json | jq ...'
#
# Optional override (rarely useful -- kept so the test bench can point
# at a fixture file without touching /etc):
#   sudo catena-restic-env --env-file /tmp/alt-backup.env restic ...

set -eu

ENV_FILE=/etc/catena/backup.env

if [ "${1:-}" = "--env-file" ]; then
    if [ -z "${2:-}" ]; then
        echo "catena-restic-env: --env-file requires a path" >&2
        exit 2
    fi
    ENV_FILE="$2"
    shift 2
fi

if [ "$#" -eq 0 ]; then
    echo "usage: catena-restic-env [--env-file PATH] CMD [ARGS...]" >&2
    exit 2
fi

if [ ! -r "$ENV_FILE" ]; then
    echo "catena-restic-env: env file not readable: $ENV_FILE" >&2
    echo "  (file is mode 0600 root:root; invoke with sudo)" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# Default HOME + XDG_CACHE_HOME so restic can locate a cache directory.
# systemd services run without HOME by default; without a cache, restic
# either fails outright (cache cannot be opened) or re-downloads repo
# indexes on every operation. Match the paths
# catena-backup.service.j2 already uses so the cache is shared
# between backup runs and auto-update rollback runs. The directories
# are created up-front by roles/backup/tasks/install.yml.
: "${HOME:=/var/lib/catena/restic-home}"
: "${XDG_CACHE_HOME:=/var/lib/catena/restic-cache}"
export HOME XDG_CACHE_HOME

exec "$@"
