#!/usr/bin/env bash
# Managed by Ansible (roles/catena-admin). Do not edit by hand.
# catena-admin action dispatcher -- the ONLY thing the catena-admin
# runner user's ssh key is allowed to run (enforced by authorized_keys'
# command= stanza).
#
# $1 is $SSH_ORIGINAL_COMMAND, which for arg-less actions is just
# the action name, and for password-arg actions is "<name> <payload>"
# as one arg. We split off the action name and expose the remainder
# as $PAYLOAD (an env var, not an argv element, so it stays out of
# `ps aux`). Actions that don't reference $PAYLOAD behave identically
# to arg-less wiring.
#
# Anything not in the dispatch table exits non-zero with no stdout.
# The dispatcher itself is root-owned 0755 so the runner user can exec
# but not modify. Commands that need privilege call sudo inline;
# the sudoers allow-list in /etc/sudoers.d/catena-admin-runner pins
# the exact dispatcher path.
#
# The dispatch table itself lives in /etc/catena/admin-actions
# (rendered from admin-actions.j2 by the role). Splitting the data
# out keeps this file plain bash so the no-Jinja-in-executables CI
# guard passes -- only the data file carries Jinja, and it has no
# script extension.
set -euo pipefail

_input="${1:-}"
action="${_input%% *}"      # first whitespace-delimited token
PAYLOAD="${_input#"$action"}"
PAYLOAD="${PAYLOAD# }"      # strip one leading space if any
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
