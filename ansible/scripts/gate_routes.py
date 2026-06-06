"""Gate-route discovery for dashboard-sync.

Walk Dokploy's projects and, for every compose/application with a domain
(outside the infra project + the auth.<zone> exemption + the Ansible-managed
infra list), write a `*-auto-gate.yml` Traefik route file under DEFAULT-DENY.
Carved out of dashboard-sync.py (see BACKLOG_TECHNICAL.md "dashboard-sync.py
decomposition"). Stdlib-only; installed beside dashboard-sync on the host.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dokploy_api  # noqa: E402
import route_synth  # noqa: E402

try:
    # In-repo (pytest / operator uv-run with pythonpath=automation).
    from helpers.labels_schema import extract_vps_auth_labels, slugify
except ModuleNotFoundError:
    # On host: labels_schema.py is installed flat beside dashboard-sync.
    from labels_schema import extract_vps_auth_labels, slugify


def sync_gate_routes(projects, api_base, api_key, dyn_dir, infra_project,
                     infra_compose_names, auth_hostname, force_https_mw,
                     proxy_port):
    """For every Dokploy compose/application with a domain (outside the
    infrastructure project + the auth.<zone> exemption + the Ansible-managed
    infra list), write a *-auto-gate.yml route file under DEFAULT-DENY:

      - public (vps.auth.mode=public or visitor) -> force-https only.
      - everything else -> a single router to the app's own per-app
        oauth2-proxy instance (oauth2-proxy-<slug>), which enforces the
        resolved --allowed-group set. Unlabeled apps resolve to DENY
        (admin-only).

    Stale + legacy (*-auto-authentik.yml) files are pruned on the same pass.

    Returns (written, removed, specs, hosts):
      specs  -- per gated app, the oauth2-proxy instance to provision
                {slug, app_name, upstream_alias, upstream_port, allowed_groups}.
      hosts  -- gated hosts needing an /oauth2/callback redirect URI."""
    dyn_dir.mkdir(parents=True, exist_ok=True)

    desired = {}
    specs = {}  # slug -> instance spec (one per app, dedup across domains)
    hosts = set()
    slug_owners = {}
    skip_names = {n.strip() for n in infra_compose_names.split(",") if n.strip()}
    seen = []

    for pname, kind, item, name in dokploy_api._iterate_deployed_items(
        projects, infra_project, skip_names, seen,
    ):
        domains = dokploy_api._fetch_domains(api_base, api_key, item, kind)
        doms = [d.get("host") for d in (domains or [])]
        if not doms:
            seen.append((pname, kind, name, doms, "skip: no-domains"))
            continue

        # Applications (single image) ship no compose labels through the
        # Dokploy API, so labels stays empty -> resolve_access DENIES them
        # (admin-only) under default-deny. That is the secure outcome.
        labels = {}
        if kind == "compose":
            compose_body = dokploy_api._fetch_compose_file(api_base, api_key, item)
            labels = extract_vps_auth_labels(compose_body)
        is_public, allowed = route_synth.resolve_access(labels, app_name=name)
        this_slug = slugify(name)

        gated_count = 0
        for d in domains or []:
            host = d.get("host")
            if not host or host == auth_hostname:
                continue
            port = d.get("port") or 80
            fname = f"{this_slug}{route_synth.AUTO_ROUTE_SUFFIX}"
            if is_public:
                body = route_synth._route_yaml_public(name, host, this_slug, port, force_https_mw)
            else:
                body = route_synth._route_yaml_perapp(name, host, force_https_mw, proxy_port)
                hosts.add(host)
                # One proxy instance per app; first domain's port is the
                # backend upstream. (Multiple domains share the instance.)
                specs.setdefault(this_slug, {
                    "slug": this_slug,
                    "app_name": name,
                    "upstream_alias": this_slug,
                    "upstream_port": port,
                    "allowed_groups": allowed,
                })

            owner = slug_owners.get(this_slug)
            if owner is not None and owner != name:
                print(
                    f"dashboard-sync/warn: slug collision -- {name!r} and "
                    f"{owner!r} both slugify to {this_slug!r}; later write "
                    f"wins. Rename one of the Dokploy apps.",
                    file=sys.stderr,
                )
            slug_owners[this_slug] = name
            desired[fname] = body
            gated_count += 1

        posture = "public" if is_public else f"groups={allowed}"
        seen.append((pname, kind, name, doms, f"routed:{gated_count} {posture}"))

    print(f"dashboard-sync: examined {len(seen)} items:")
    for row in seen:
        print(f"  {row[4]:24s} project={row[0]!r:18s} kind={row[1]:11s} "
              f"name={row[2]!r:20s} domains={row[3]}")

    written = 0
    for fname, body in desired.items():
        path = dyn_dir / fname
        if path.exists() and path.read_text(encoding="utf-8") == body:
            continue
        path.write_text(body, encoding="utf-8")
        written += 1

    removed = 0
    for suffix in (route_synth.AUTO_ROUTE_SUFFIX, route_synth._LEGACY_AUTO_ROUTE_SUFFIX):
        for path in dyn_dir.glob(f"*{suffix}"):
            if path.name not in desired:
                path.unlink()
                removed += 1

    return written, removed, list(specs.values()), hosts
