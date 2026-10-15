#!/usr/bin/env bash
# catena-admin action dispatcher -- the ONLY thing the catena-admin
# runner user's ssh key is allowed to run (enforced by authorized_keys'
# command= stanza, which runs catena-dispatch-entry.sh beside it).
#
# Installed per generation of the dispatch path by reconcile/roles/dispatch_path
# (helpers/dispatch_path.py), which proves a generation before the panel's key
# is pointed at it. /usr/local/sbin/catena-admin-runner links to the running
# one. Do not edit by hand.
#
# $1 is the action name and nothing else: the entry refuses a request carrying
# more before it runs sudo, which logs the command line it runs. A request's
# payload rides the CATENA_DISPATCH_PAYLOAD environment variable instead (sshd
# AcceptEnv and the sudoers env_keep pass it, both rendered from
# catena_admin_dispatch_env_passthrough) and is exposed as $PAYLOAD. A command
# line carrying anything after the name is refused here too, for root callers
# that run this file directly, and the refusal names the action alone. Actions
# that don't reference $PAYLOAD behave identically to arg-less wiring.
#
# Anything not in the dispatch table exits non-zero with no stdout.
# The dispatcher itself is root-owned 0755 so the runner user can exec
# but not modify. Commands that need privilege call sudo inline;
# the runner's sudo rule names this generation's dispatcher alone.
#
# The dispatch table itself lives in /etc/catena/admin-actions
# (rendered from admin-actions.j2 by reconcile/roles/catena-admin). Splitting the data
# out keeps this file plain bash so the no-Jinja-in-executables CI
# guard passes -- only the data file carries Jinja, and it has no
# script extension.
set -euo pipefail

action="${1:-}"
if [ "$#" -gt 1 ] || [[ "$action" == *[[:space:]]* ]]; then
    echo "catena-admin-runner: refusing '${action%%[[:space:]]*}': a request's payload rides CATENA_DISPATCH_PAYLOAD, never the command line" >&2
    exit 2
fi
PAYLOAD="${CATENA_DISPATCH_PAYLOAD:-}"
export PAYLOAD

# The proof helpers/dispatch_path.py dispatches through a generation before the
# panel's key is pointed at it: which generation answered, and the name of every
# variable that arrived holding the payload's value. It prints names, never a
# value, and answers before the dispatch table, which a first install has not
# written yet.
if [ "$action" = "dispatch-path-proof" ]; then
    echo "dispatch-path-proof $(basename "$(dirname "$(readlink -f "$0")")")"
    if [ -n "$PAYLOAD" ]; then
        for name in $(compgen -e); do
            if [ "$name" != PAYLOAD ] && [ "${!name}" = "$PAYLOAD" ]; then
                echo "arrived $name"
            fi
        done
    fi
    exit 0
fi

ACTIONS_FILE="${CATENA_ADMIN_ACTIONS_FILE:-/etc/catena/admin-actions}"
if [ ! -f "$ACTIONS_FILE" ]; then
    echo "catena-admin-runner: dispatch table missing at $ACTIONS_FILE" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$ACTIONS_FILE"

if ! declare -F catena_admin_dispatch >/dev/null 2>&1; then
    echo "catena-admin-runner: dispatch table at $ACTIONS_FILE did not define catena_admin_dispatch()" >&2
    exit 1
fi

catena_admin_dispatch "$action"
