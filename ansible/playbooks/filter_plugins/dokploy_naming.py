"""Ansible filters for Dokploy's container-naming convention.

Dokploy generates container names as:

    <compose-name>-<6-char-hash>-<service-name>-<index>

where:
- <compose-name>  = the compose's appName (catalog field) or display name
- <6-char-hash>   = a per-compose-instance random suffix; rotates only
                    when the compose is recreated, not on redeploy
- <service-name>  = the key from the compose file's `services:` block
- <index>         = 1 for non-replicated services; higher for scaled
                    deployments

Many tasks need to find a running container by `(compose, service)`
without knowing the hash. The pattern is duplicated across ~10
locations in the codebase as a literal regex with the hash placeholder
inlined. Centralising into a filter eliminates the literal duplication
and makes a future Dokploy naming-convention change a one-file edit.

Usage in shell-pipe form:

    docker ps --format '{{ "{{" }}.Names{{ "}}" }}' \\
      | grep -E '{{ "gatus" | dokploy_container_regex("app") }}' \\
      | head -n1

The filter returns the anchored regex (^...$). Callers wrap it in
their own pipeline + decide how to handle no-match (fail loudly,
silent skip, etc.).

Naming filter rationale: Python identifier collision with
'dokploy_container_pattern' would clash with potential future filters;
'_regex' makes the return value's shape explicit at every call site.
"""

from __future__ import annotations

import re


def _validate_token(name: str, kind: str) -> str:
    """Reject obviously-wrong inputs early.

    A compose / service name must be a non-empty Docker-compatible
    token (lowercase alphanumerics + dashes + underscores). If a caller
    passes empty string or something with regex meta-characters, the
    resulting regex would either silently match nothing (empty = `^-`)
    or break the shell pipeline. Fail at filter time so the operator
    sees the bad input AT the failing task, not via a confusing
    silent-skip three steps later."""
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"dokploy_container_regex: {kind} must be a non-empty string "
            f"(got {name!r})"
        )
    # Docker compose / service names allow [a-z0-9_.-] in practice. We
    # accept the same set here so a typo with an uppercase letter or
    # space surfaces immediately.
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ValueError(
            f"dokploy_container_regex: {kind}={name!r} contains characters "
            f"that aren't valid in a Docker compose / service name"
        )
    return name


def dokploy_container_regex(compose_name, service_name="app", index=1):
    """Return the anchored regex matching Dokploy's container-name
    pattern for a `(compose, service, index)` triple.

    Defaults: service_name="app" (the canonical name for single-
    container Dokploy templates), index=1 (no replicas)."""
    compose = _validate_token(compose_name, "compose_name")
    service = _validate_token(service_name, "service_name")
    if not isinstance(index, int) or index < 1:
        raise ValueError(
            f"dokploy_container_regex: index must be a positive int "
            f"(got {index!r})"
        )
    return f"^{re.escape(compose)}-[a-z0-9]+-{re.escape(service)}-{index}$"


class FilterModule:
    def filters(self):
        return {
            "dokploy_container_regex": dokploy_container_regex,
        }
