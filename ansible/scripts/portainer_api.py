"""Portainer stack API + generic JSON-HTTP helpers for dashboard-sync.

Stdlib-only (urllib) so the host runs it without extra Python deps.
Installed beside dashboard-sync on the host by
roles/infrastructure/tasks/dashboard_sync.yml.

Portainer facts that shape this module:
  - Auth header is `X-API-Key`.
  - No project/environment grouping -- stacks are flat, grouped by Name.
  - Stack ops are scoped to an endpoint id (?endpointId=), resolved from
    GET /api/endpoints (the local Docker env roles/portainer creates).
  - Client-app hostnames come from the compose `vps.route.host` label
    (labels_schema.extract_vps_route_labels), not a domain API.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

# ${VAR}, ${VAR:-default}, ${VAR-default} compose-interpolation refs. Group 1
# is the name; group 2 is the default separator (present only when a default
# is supplied); group 3 is the default text.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)((?::-)|-)?([^}]*)\}")
_DOLLAR_ESCAPE = "\x00CATENA_DOLLAR\x00"


def resolve_compose_env(text, env):
    """Substitute ${VAR} / ${VAR:-default} / ${VAR-default} in `text` using
    `env` (name->value), mirroring docker-compose interpolation for the
    label-reading path.

    Portainer stores a stack's compose file VERBATIM (with `${...}` refs) and
    its resolved values in a separate Env array; docker expands the refs only
    at `compose up`. dashboard-sync reads the stored file, so a client app's
    `vps.route.host=${DOMAIN_HOST}` would otherwise reach gate_routes as the
    literal string `${DOMAIN_HOST}`. Resolving here lets the route/auth/alias
    extractors see the same values docker does.

    `$$` is an escaped literal `$`. An unset `${VAR}` with no default is left
    verbatim so a downstream gap stays visible (and no host is silently
    blanked)."""
    if not text:
        return text
    t = text.replace("$$", _DOLLAR_ESCAPE)

    def _repl(m):
        name, sep, default = m.group(1), m.group(2), m.group(3)
        if name in env:
            return env[name]
        if sep:  # a default was supplied (${VAR:-x} or ${VAR-x})
            return default
        return m.group(0)  # unset, no default -> leave ${VAR} literal

    return _ENV_REF_RE.sub(_repl, t).replace(_DOLLAR_ESCAPE, "$")


def portainer_get(base, path, api_key):
    """GET a Portainer API path with the X-API-Key header. Exits(3) on any
    HTTP/transport error (a single misbehaving call should fail the sync run
    loudly rather than silently write a partial route set)."""
    req = urllib.request.Request(
        f"{base}{path}",
        headers={"X-API-Key": api_key, "accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"dashboard-sync: HTTP {e.code} on GET {path}: "
              f"{e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(3)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"dashboard-sync: request failed on GET {path}: {e}", file=sys.stderr)
        sys.exit(3)


def http_json(url, headers, body=None, method="POST", timeout=30):
    """POST/PUT JSON (or form when body is str) and return parsed JSON or
    {}. Raises RuntimeError on HTTP error so callers can decide whether a
    failure is fatal (stack provisioning) or skippable (redirect sync)."""
    data = None
    hdrs = dict(headers)
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("content-type", "application/json")
    elif isinstance(body, str):
        data = body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} on {method} {url}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"request failed on {method} {url}: {e}") from e


# ─── Portainer stack helpers ──────────────────────────────────────────────
def portainer_endpoint_id(api_base, api_key):
    """Resolve the local Docker environment id (prefer Type==1, else first)."""
    endpoints = portainer_get(api_base, "/endpoints", api_key)
    if not isinstance(endpoints, list) or not endpoints:
        print("dashboard-sync: Portainer has no environments (endpoints)",
              file=sys.stderr)
        sys.exit(3)
    docker = [e for e in endpoints if isinstance(e, dict) and e.get("Type") == 1]
    return int((docker or endpoints)[0]["Id"])


def list_stacks(api_base, api_key):
    """GET /api/stacks -> list of stack objects (Id, Name, Status, Env, ...)."""
    result = portainer_get(api_base, "/stacks", api_key)
    return result if isinstance(result, list) else []


def stack_file(api_base, api_key, stack_id):
    """GET /api/stacks/{id}/file -> the StackFileContent (compose body)."""
    try:
        full = portainer_get(api_base, f"/stacks/{stack_id}/file", api_key)
    except SystemExit:
        return ""
    if isinstance(full, dict):
        return full.get("StackFileContent") or ""
    return ""


def _stack_env_map(stack):
    """Build a {name: value} map from a Portainer stack's Env array
    ([{name, value}, ...]) so the compose body can be resolved the way docker
    would at `compose up`."""
    env = {}
    for ev in stack.get("Env") or []:
        if isinstance(ev, dict) and ev.get("name"):
            env[ev["name"]] = ev.get("value", "")
    return env


def iter_stacks(api_base, api_key, skip_names, seen=None):
    """Yield `(name, stack_id, compose_body)` for every stack whose Name is
    not in `skip_names` (the infra stacks Ansible manages directly). `seen`,
    if given, records `(name, status, note)` rows so the sync debug trail
    stays complete. Only ACTIVE stacks (Status==1) are yielded -- an
    inactive/never-deployed stack has no live backend to route to.

    The compose body is env-resolved against the stack's stored Env array
    (resolve_compose_env), so `${VAR}` refs in labels -- notably
    `vps.route.host=${DOMAIN_HOST}` -- reach the label extractors as the value
    docker deploys, not the literal `${...}` Portainer keeps in the file."""
    for stack in list_stacks(api_base, api_key):
        name = stack.get("Name")
        if not name:
            continue
        if name in skip_names:
            if seen is not None:
                seen.append((name, stack.get("Status"), "skip: infra-list"))
            continue
        if stack.get("Status") != 1:
            if seen is not None:
                seen.append((name, stack.get("Status"), "skip: inactive"))
            continue
        body = stack_file(api_base, api_key, stack.get("Id"))
        body = resolve_compose_env(body, _stack_env_map(stack))
        yield name, stack.get("Id"), body
