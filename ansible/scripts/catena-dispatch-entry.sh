#!/usr/bin/env bash
# The forced command of the panel's dispatch: what sshd runs, as the runner
# account, for every request the panel sends.
#
# Installed per generation of the dispatch path by reconcile/roles/dispatch_path
# (helpers/dispatch_path.py), beside the dispatcher it hands the request to, and
# named by the command= option of the panel key's authorized_keys line.
#
# The request is $SSH_ORIGINAL_COMMAND, and it is one action name: letters,
# digits, dot, underscore, hyphen. sudo logs the command line it runs and the
# process table shows it, so a request carrying anything more is refused here,
# with exit 2, before sudo runs; a request's payload rides
# CATENA_DISPATCH_PAYLOAD. The refusal names the request's first word only when
# that word is itself an action name.
set -euo pipefail

name_re='^[A-Za-z0-9._-]+$'
request="${SSH_ORIGINAL_COMMAND:-}"
if [[ ! "$request" =~ $name_re ]]; then
    first="${request%%[[:space:]]*}"
    if [[ "$first" =~ $name_re ]]; then
        echo "catena-dispatch-entry: refusing '$first': a request carries the action name alone, its payload rides CATENA_DISPATCH_PAYLOAD" >&2
    else
        echo "catena-dispatch-entry: refusing a request that is not an action name, its payload rides CATENA_DISPATCH_PAYLOAD" >&2
    fi
    exit 2
fi

here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec /usr/bin/sudo -n "$here/dispatcher" "$request"
