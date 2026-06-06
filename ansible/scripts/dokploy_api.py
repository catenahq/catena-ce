"""Dokploy REST + generic JSON-HTTP helpers for dashboard-sync.

Stdlib-only (urllib) so the host runs it without extra Python deps. Carved
out of dashboard-sync.py (see BACKLOG_TECHNICAL.md "dashboard-sync.py
decomposition") and installed beside it on the host by
roles/infrastructure/tasks/dashboard_sync.yml. See dashboard-sync.py for the
overall design + env-var contract.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def dokploy_get(base, path, api_key):
    req = urllib.request.Request(
        f"{base}{path}",
        headers={"x-api-key": api_key, "accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"dashboard-sync: HTTP {e.code} on GET {path}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(3)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"dashboard-sync: request failed on GET {path}: {e}", file=sys.stderr)
        sys.exit(3)


def http_json(url, headers, body=None, method="POST", timeout=30):
    """POST/PUT JSON (or form when body is str) and return parsed JSON or
    {}. Raises RuntimeError on HTTP error so callers can decide whether a
    failure is fatal (compose provisioning) or skippable (redirect sync)."""
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


def dokploy_post(base, path, api_key, body):
    return http_json(
        f"{base}{path}",
        headers={"x-api-key": api_key, "accept": "application/json"},
        body=body,
        method="POST",
    )


def _fetch_domains(api_base, api_key, item, kind):
    """Dokploy's /project.all returns compose/application objects WITHOUT
    their domain records -- you have to call domain.byComposeId /
    domain.byApplicationId separately. Do that here and return the list."""
    endpoint = "domain.byComposeId" if kind == "compose" else "domain.byApplicationId"
    id_field = "composeId" if kind == "compose" else "applicationId"
    item_id = item.get(id_field)
    if not item_id:
        return []
    try:
        return dokploy_get(api_base, f"/{endpoint}?{id_field}={item_id}", api_key)
    except SystemExit:
        # dokploy_get exits on HTTP error; treat as no-domains to avoid
        # halting a whole sync run over one misbehaving compose.
        return []


def _fetch_compose_file(api_base, api_key, item):
    """Dokploy's /project.all returns compose objects with metadata only;
    the compose body (where vps.auth.* labels live) is fetched via
    compose.one?composeId=... -- same pattern as _fetch_domains. Returns
    the composeFile string, or empty string if unavailable."""
    compose_id = item.get("composeId")
    if not compose_id:
        return ""
    try:
        full = dokploy_get(api_base, f"/compose.one?composeId={compose_id}", api_key)
    except SystemExit:
        return ""
    if not isinstance(full, dict):
        return ""
    return full.get("composeFile") or ""


def _iterate_deployed_items(projects, infra_project, skip_names, seen):
    """Flatten Dokploy's project -> env -> (applications|compose) tree
    into a stream of `(project_name, kind, item, name)` tuples. Skipped
    projects / items are recorded in `seen` so the debug trail remains
    complete."""
    for proj in projects:
        pname = proj.get("name")
        if pname == infra_project:
            seen.append((pname, "PROJECT", "-", "-", "skip: infra-project"))
            continue
        envs = proj.get("environments", [])
        if not envs:
            seen.append((pname, "PROJECT", "-", "-", "skip: no-envs"))
            continue
        env = envs[0]
        for kind, items in (
            ("application", env.get("applications", []) or []),
            ("compose", env.get("compose", []) or []),
        ):
            for item in items:
                name = item.get("appName") or item.get("name") or "<noname>"
                if name in skip_names:
                    seen.append((pname, kind, name, [], "skip: infra-list"))
                    continue
                yield pname, kind, item, name
