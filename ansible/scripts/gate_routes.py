"""Gate-route discovery for dashboard-sync.

Walk the Portainer stacks and, for every stack that declares a public host
(`vps.route.host`, outside the auth.<zone> exemption + the Ansible-managed
infra list), write a `*-auto-gate.yml` Traefik route file under DEFAULT-DENY.
Stdlib-only; installed beside dashboard-sync on the host.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portainer_api  # noqa: E402
import route_synth  # noqa: E402

try:
    # In-repo (pytest / operator uv-run with pythonpath=automation).
    from helpers.labels_schema import (
        extract_service_aliases,
        extract_vps_auth_labels,
        extract_vps_route_labels,
        slugify,
    )
except ModuleNotFoundError:
    # On host: labels_schema.py is installed flat beside dashboard-sync.
    from labels_schema import (
        extract_service_aliases,
        extract_vps_auth_labels,
        extract_vps_route_labels,
        slugify,
    )


def derive_route_intent(api_base, api_key, skip_names, auth_hostname,
                        seen=None):
    """Yield one intent dict per routed Portainer stack.

    The SINGLE source of truth for what dashboard-sync writes AND what
    validate verifies (verify_gated_intent.py): both consume this walk,
    so the verifier can never drift from the writer -- a stack whose
    gate file was removed or renamed still shows up here and fails the
    verify.

    Each dict: {name, host, is_public, allowed, slug, backend_alias,
    route_slug, port, fname}."""
    for name, _stack_id, compose_body in portainer_api.iter_stacks(
        api_base, api_key, skip_names, seen=seen,
    ):
        route = extract_vps_route_labels(compose_body)
        host = route.get("host")
        if not host or host == auth_hostname:
            if seen is not None:
                seen.append((name, host, "skip: no-route-host"))
            continue

        labels = extract_vps_auth_labels(compose_body)
        # service -> [catena-network aliases]: a host that fronts a non-primary
        # service (Talk HPB's signaling.<zone>) must route to THAT service's
        # alias, not the stack-name slug (which only the primary carries).
        svc_aliases = extract_service_aliases(compose_body)
        is_public, allowed = route_synth.resolve_access(labels, app_name=name)
        this_slug = slugify(name)

        port = route.get("port") or 80
        # Backend alias = the catena-network alias of the service this host
        # fronts (vps.route.service). Falls back to the stack-name slug for a
        # single-service app or when the service declares no alias.
        svc_name = (route.get("service") or "").strip()
        svc_alias_list = svc_aliases.get(svc_name) if svc_name else None
        backend_alias = svc_alias_list[0] if svc_alias_list else this_slug
        route_slug = (
            this_slug if backend_alias == this_slug else slugify(backend_alias)
        )
        yield {
            "name": name,
            "host": host,
            "is_public": is_public,
            "allowed": allowed,
            "slug": this_slug,
            "backend_alias": backend_alias,
            "route_slug": route_slug,
            "port": port,
            "fname": f"{route_slug}{route_synth.AUTO_ROUTE_SUFFIX}",
        }


def sync_gate_routes(api_base, api_key, dyn_dir, infra_compose_names,
                     auth_hostname, force_https_mw, proxy_port):
    """For every active Portainer stack that declares `vps.route.host`
    (outside the auth.<zone> exemption + the Ansible-managed infra list),
    write a *-auto-gate.yml route file under DEFAULT-DENY:

      - public (vps.auth.mode=public or visitor) -> force-https only.
      - everything else -> a single router to the app's own per-app
        oauth2-proxy instance (oauth2-proxy-<slug>), which enforces the
        resolved --allowed-group set. Unlabeled apps resolve to DENY
        (admin-only).

    Stale *-auto-gate.yml files are pruned on the same pass.

    Returns (written, removed, specs, hosts):
      specs  -- per gated app, the oauth2-proxy instance to provision
                {slug, app_name, upstream_alias, upstream_port, allowed_groups}.
      hosts  -- gated hosts needing an /oauth2/callback redirect URI."""
    dyn_dir.mkdir(parents=True, exist_ok=True)

    desired = {}
    specs = {}  # slug -> instance spec (one per app)
    hosts = set()
    slug_owners = {}
    skip_names = {n.strip() for n in infra_compose_names.split(",") if n.strip()}
    seen = []

    for intent in derive_route_intent(
        api_base, api_key, skip_names, auth_hostname, seen=seen,
    ):
        name = intent["name"]
        host = intent["host"]
        route_slug = intent["route_slug"]
        this_slug = intent["slug"]
        if intent["is_public"]:
            body = route_synth._route_yaml_public(
                name, host, intent["backend_alias"], intent["port"],
                force_https_mw, route_slug=route_slug,
            )
        else:
            body = route_synth._route_yaml_perapp(
                name, host, force_https_mw, proxy_port, route_slug=route_slug,
            )
            hosts.add(host)
            specs.setdefault(this_slug, {
                "slug": this_slug,
                "app_name": name,
                "host": host,
                "upstream_alias": this_slug,
                "upstream_port": intent["port"],
                "allowed_groups": intent["allowed"],
            })

        owner = slug_owners.get(route_slug)
        if owner is not None and owner != name:
            print(
                f"dashboard-sync/warn: slug collision -- {name!r} and "
                f"{owner!r} both slugify to {route_slug!r}; later write "
                f"wins. Rename one of the stacks.",
                file=sys.stderr,
            )
        slug_owners[route_slug] = name
        desired[intent["fname"]] = body
        posture = (
            "public" if intent["is_public"] else f"groups={intent['allowed']}"
        )
        seen.append((name, host, f"routed {posture}"))

    print(f"dashboard-sync: examined {len(seen)} stacks:")
    for row in seen:
        print(f"  {row[2]:24s} stack={row[0]!r:22s} host={row[1]}")

    written = 0
    for fname, body in desired.items():
        path = dyn_dir / fname
        if path.exists() and path.read_text(encoding="utf-8") == body:
            continue
        path.write_text(body, encoding="utf-8")
        written += 1

    removed = 0
    for path in dyn_dir.glob(f"*{route_synth.AUTO_ROUTE_SUFFIX}"):
        if path.name not in desired:
            path.unlink()
            removed += 1

    return written, removed, list(specs.values()), hosts
