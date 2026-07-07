"""Ansible filter for Portainer's container-naming convention.

Portainer deploys a standalone compose stack with the stack Name as the
compose project, so it generates container names as:

    <stack-name>-<service-name>-<index>

where:
- <stack-name>    = the Portainer stack Name (== the compose project label)
- <service-name>  = the key from the compose file's `services:` block
- <index>         = 1 for non-replicated services; higher for scaled

(The previous control plane used `<compose>-<6hex>-<service>-<index>` with a
per-instance hash; that hashed form is gone.)

Many tasks need to find a running container by `(stack, service)`. The pattern
is centralised here so a future naming-convention change is a one-file edit.

Usage in shell-pipe form:

    docker ps --format '{{ "{{" }}.Names{{ "}}" }}' \\
      | grep -E '{{ "gatus" | portainer_container_regex("app") }}' \\
      | head -n1

The filter returns the anchored regex (^...$). Callers wrap it in their own
pipeline + decide how to handle no-match (fail loudly, silent skip, etc.).
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
            f"portainer_container_regex: {kind} must be a non-empty string "
            f"(got {name!r})"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ValueError(
            f"portainer_container_regex: {kind}={name!r} contains characters "
            f"that aren't valid in a Docker compose / service name"
        )
    return name


def portainer_container_regex(stack_name, service_name="app", index=1):
    """Return the anchored regex matching Portainer's container-name pattern
    for a `(stack, service, index)` triple.

    Container names are `<stack>-<service>-<index>` (Compose v2 hyphen
    separators, no per-instance hash). Defaults: service_name="app", index=1."""
    stack = _validate_token(stack_name, "stack_name")
    service = _validate_token(service_name, "service_name")
    if not isinstance(index, int) or index < 1:
        raise ValueError(
            f"portainer_container_regex: index must be a positive int "
            f"(got {index!r})"
        )
    return f"^{re.escape(stack)}-{re.escape(service)}-{index}$"


class FilterModule:
    def filters(self):
        return {
            "portainer_container_regex": portainer_container_regex,
        }
