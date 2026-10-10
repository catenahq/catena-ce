#!/usr/bin/env bash
# catena-admin action dispatcher -- the ONLY thing the catena-admin
# runner user's ssh key is allowed to run (enforced by authorized_keys'
# command= stanza).
#
# Managed by Ansible (bootstrap/roles/catena_admin_host). Do not edit by hand.
#
# $1 is $SSH_ORIGINAL_COMMAND, which is the action name and nothing else.
# sudo logs the command line it runs, and the process table shows it, so a
# request's payload rides the CATENA_DISPATCH_PAYLOAD environment variable
# instead (sshd AcceptEnv and the sudoers env_keep pass it, both rendered from
# catena_admin_dispatch_env_passthrough) and is exposed as $PAYLOAD. A command
# line carrying anything after the name is refused before any action runs, and
# the refusal names the action alone. Actions that don't reference $PAYLOAD
# behave identically to arg-less wiring.
#
# Anything not in the dispatch table exits non-zero with no stdout.
# The dispatcher itself is root-owned 0755 so the runner user can exec
# but not modify. Commands that need privilege call sudo inline;
# the sudoers allow-list in /etc/sudoers.d/catena-admin-runner pins
# the exact dispatcher path.
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
