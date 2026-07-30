"""Ansible filter for swarm's task-container naming convention.

`docker stack deploy` creates one service per compose service key, named
`<stack>_<service>`, and swarm names each task container:

    <stack>_<service>.<slot>.<task-id>

where:
- <stack>   = the stack name passed to `docker stack deploy` (also the
              compose project label on the container)
- <service> = the key from the compose file's `services:` block
- <slot>    = 1-based replica slot (1 for an unscaled service)
- <task-id> = swarm's per-task id, regenerated on every restart

The task id is the reason this is a regex and not a format string: it
changes whenever swarm recreates the task, so nothing can hold a container
name across a restart. Every caller re-resolves by `(stack, service)` at
use time.

This replaced the Portainer form `<stack>-<service>-<index>` when the
catena-declared apps moved off the Portainer stack API. The two are not
compatible -- Portainer's compose project used hyphens throughout, swarm
uses an underscore before the service and dots around the task id -- so a
host converged by an older build has containers this pattern will not
match. That is correct: those containers are the Portainer-deployed ones,
and matching them would let a task operate on the stack it just replaced.

Usage in shell-pipe form:

    docker ps --format '{{ "{{" }}.Names{{ "}}" }}' \\
      | grep -E '{{ "gatus" | swarm_container_regex("app") }}' \\
      | head -n1

The filter returns the anchored regex (^...$). Callers wrap it in their own
pipeline + decide how to handle no-match (fail loudly, silent skip, etc.).

End-to-end coverage:
    catena-ce ansible/tests/unit/test_swarm_naming.py
"""

from __future__ import annotations

import re


def _validate_token(name: str, kind: str) -> str:
    """Reject obviously-wrong inputs early.

    A stack / service name must be a non-empty Docker-compatible token
    (alphanumerics + dashes + underscores + dots). Empty string or regex
    meta-characters would silently match nothing or break the shell pipeline,
    so fail at filter time -- the operator sees the bad input AT the failing
    task, not via a confusing silent-skip three steps later."""
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"swarm_container_regex: {kind} must be a non-empty string "
            f"(got {name!r})"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ValueError(
            f"swarm_container_regex: {kind}={name!r} contains characters "
            f"that aren't valid in a Docker compose / service name"
        )
    return name


def swarm_container_regex(stack_name, service_name="app", slot=1):
    """Return the anchored regex matching a swarm task container for a
    `(stack, service, slot)` triple.

    Defaults: service_name="app", slot=1 (the only slot of an unscaled
    service)."""
    stack = _validate_token(stack_name, "stack_name")
    service = _validate_token(service_name, "service_name")
    if not isinstance(slot, int) or slot < 1:
        raise ValueError(
            f"swarm_container_regex: slot must be a positive int "
            f"(got {slot!r})"
        )
    return rf"^{re.escape(stack)}_{re.escape(service)}\.{slot}\.[a-z0-9]+$"


class FilterModule:
    def filters(self):
        return {
            "swarm_container_regex": swarm_container_regex,
        }
