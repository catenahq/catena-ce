"""Ansible filter: is a failed `docker stack deploy` attempt worth another one.

    swarm_deploy_transient(rc, output, tasks, converged=False) -> bool

`rc` and `output` (stdout then stderr) are the attempt's own;
`tasks` is the per-task state reconcile/roles/infrastructure/tasks/
_swarm_stack_deploy_attempt.yml collects (`docker service ps --no-trunc`,
CurrentState and Error of every task of the stack); `converged` says the
stack reached its desired replicas although the deploy exited non-zero.

A non-zero exit is transient when the output or the task state carries a
registry or daemon transient. `docker stack deploy --detach=false` reports
some per-task errors in its stdout progress stream and CLI failures on
stderr, and when swarm pauses a rolling update its stderr names only a task
id: the cause is that task's Error, which only the task state carries.

A timeout (rc 124, `timeout` killing the attempt) is transient only while the
task state shows an image pull that has not finished: a task still Preparing
(swarm pulls the image in that state), or a task whose pull failed, or a
registry transient. A task that can never converge for another reason, a
crash loop (`task: non-zero exit (1)`) or a task no node accepts (`no
suitable node`), fails at the first timeout instead of spending every attempt
on the same wait. The killed progress stream is not read: it carries whatever
it printed last, which on a stalled pull is nothing.
"""

from __future__ import annotations

import re

TIMED_OUT = 124

# Registry and daemon transients. A swarm manager past its own deadline
# answers "context deadline exceeded" or the gRPC "code = DeadlineExceeded";
# "update out of sequence" follows either, because the timed-out update still
# commits and the retry sends a stale spec version. "Address already in use"
# is a leaked overlay endpoint the attempt file reclaims before the retry.
TRANSIENT = re.compile(
    r"502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout"
    r"|TLS handshake timeout|connection reset|i/o timeout|TOOMANYREQUESTS"
    r"|context deadline exceeded|code = DeadlineExceeded"
    r"|temporary failure in name resolution"
    r"|received unexpected HTTP status: 5|did not receive an exit event"
    r"|cannot stop container|unexpected EOF|short read: expected"
    r"|Address already in use|update out of sequence"
)

# An image pull that has not finished. Docker fails a task whose pull failed
# with the pull error itself, or with "No such image" from the container
# create that follows the pull.
PULLING = re.compile(
    r"\bPreparing\b|No such image|pull access denied|error pulling"
    r"|no matching manifest|failed to do request|failed to authorize"
)


def swarm_deploy_transient(rc, output, tasks, converged=False):
    output = str(output or "")
    tasks = str(tasks or "")
    if converged:
        return True
    if int(rc) == TIMED_OUT:
        return bool(TRANSIENT.search(tasks) or PULLING.search(tasks))
    return bool(TRANSIENT.search(output + tasks))


class FilterModule:
    def filters(self):
        return {"swarm_deploy_transient": swarm_deploy_transient}
