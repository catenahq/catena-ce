#!/usr/bin/env python3
"""Regenerate Gatus endpoint config (50-dokploy-apps.yaml) from
Dokploy's project / compose / domain APIs. Emits two endpoints per
non-infrastructure app: an internal alias check (expects 200-ish)
and a public-domain check (expects auth-redirect / 302). Runs via
systemd timer + on-demand from catena-admin."""
# Managed by Ansible (roles/infrastructure). Do not edit by hand.
# /usr/local/bin/gatus-sync -- regenerate
# $GATUS_CONFIG_PATH (default .../50-dokploy-apps.yaml) from Dokploy's project /
# compose / application / domain APIs. For every non-infrastructure Dokploy
# app with at least one domain we emit TWO Gatus endpoints:
#
#   <app>-internal : http://<network-alias>:<port>/   (conditions: 200-ish)
#   <app>-public   : https://<host>/                  (conditions: 302, auth redirect)
#
# "Ensure minimum, never delete modifications" (user directive, 2026-04-17):
# this script owns ONLY 50-dokploy-apps.yaml. Operator additions go in
# 99-*.yaml files which Gatus also loads but this script never touches.
#
# Stdlib-only (urllib, subprocess). Triggered by systemd timer +
# catena-admin action + any Ansible handler on the gatus role.
#
# When the generated file changes, the Gatus container is restarted so the
# new endpoint list takes effect immediately (Gatus reads config only at
# startup; there's no in-process reload endpoint).
#
# Env vars (/etc/catena/gatus-sync.env):
#   DOKPLOY_API_BASE, DOKPLOY_API_KEY, DOKPLOY_INFRA_PROJECT
#   GATUS_CONFIG_PATH          -- full path of 50-dokploy-apps.yaml
#   GATUS_CONTAINER_NAME_RE    -- regex to find Gatus container name in docker ps
#   INFRA_COMPOSE_NAMES        -- comma-separated appNames owned by Ansible
#                                (already covered in 00-base.yaml; skipped here)
#   AUTH_HOSTNAME              -- never monitor auth.<zone> here (it's in base)
#   GATUS_SUMMARY_PATH         -- write a small JSON summary ({total, up, down,
#                                down_names, oldest_failure_at, checked_at})
#                                here for the Homepage customapi widget. If
#                                unset or the Gatus API is unreachable, skip
#                                silently -- this output is best-effort display.
#   GATUS_API_URL              -- base URL of Gatus API, e.g.
#                                http://gatus:8080 (network-internal alias).

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _env(name, default=None, required=True):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"gatus-sync: missing env var {name}", file=sys.stderr)
        sys.exit(2)
    return v


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
        print(f"gatus-sync: HTTP {e.code} on GET {path}: "
              f"{e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(3)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"gatus-sync: request failed on GET {path}: {e}", file=sys.stderr)
        sys.exit(3)


def _fetch_domains(api_base, api_key, item, kind):
    endpoint = "domain.byComposeId" if kind == "compose" else "domain.byApplicationId"
    id_field = "composeId" if kind == "compose" else "applicationId"
    item_id = item.get(id_field)
    if not item_id:
        return []
    try:
        return dokploy_get(api_base, f"/{endpoint}?{id_field}={item_id}", api_key)
    except SystemExit:
        return []


_SLUG_SAFE = re.compile(r"[^a-z0-9-]+")


def _slug(s: str) -> str:
    """Return a Healthchecks-safe slug: lowercase, only [a-z0-9-],
    collapse dashes, strip ends. Used as the [ALERT_DESCRIPTION]
    placeholder in Gatus's custom-alert URL template so each endpoint
    gets its own Healthchecks check (gatus-<slug>)."""
    s = _SLUG_SAFE.sub("-", s.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unnamed"


def _endpoint_yaml(name, group, url, accepted, description):
    """Render one Gatus endpoint entry. `accepted` is a list of int HTTP
    codes that count as healthy. `description` is the per-endpoint slug
    consumed as the [ALERT_DESCRIPTION] placeholder in the custom-alert
    URL (see gatus-base.yaml.j2 alerting.custom)."""
    cond = (
        f"[STATUS] == {accepted[0]}"
        if len(accepted) == 1
        else f"[STATUS] == any({', '.join(str(c) for c in accepted)})"
    )
    return (
        f"  - name: {name}\n"
        f"    group: {group}\n"
        f'    url: "{url}"\n'
        f"    interval: 120s\n"
        f"    client:\n"
        f"      ignore-redirect: true\n"
        f"    conditions:\n"
        f'      - "{cond}"\n'
        f"    alerts:\n"
        f"      - type: custom\n"
        f'        description: "{description}"\n'
    )


def load_version_map(path: Path) -> dict[str, dict]:
    """Read version-check.json and return {service_name: {image, current,
    latest, outdated, eligible}}. Used to format endpoint NAMES as
    `<title> -- <image> <current> [(<latest>)]`. Empty dict on error
    (endpoints render without version annotation)."""
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for svc in data.get("services") or []:
        name = (svc.get("name") or "").strip()
        if not name:
            continue
        full_image = (svc.get("full_image") or "").strip()
        # Strip registry prefix (docker.io/..., ghcr.io/...) + drop the
        # tag. Keeps things compact on the subtitle line.
        image_repo = full_image
        if image_repo.startswith("docker.io/"):
            image_repo = image_repo[len("docker.io/"):]
        if ":" in image_repo:
            image_repo = image_repo.rsplit(":", 1)[0]
        # Display name comes from the SERVICE_SPECS project name
        # (e.g., "Keycloak", "Traefik (Dokploy)"), lowercased and
        # with any trailing paren-suffix stripped. Also strip the
        # `<org>/` prefix that client-app entries carry (version-check
        # writes them as `actualbudget/actualbudget` from the image
        # repo) so cards show `actualbudget`, not `actualbudget/actualbudget`.
        # Sidesteps generic image last-segments like "phasetwo-keycloak"
        # (for quay.io/phasetwo/phasetwo-keycloak) without per-service
        # overrides.
        display_name = name.lower().split(" (")[0].rsplit("/", 1)[-1]
        out[name] = {
            "image": image_repo,
            "display_name": display_name,
            "current": (svc.get("current") or "").strip(),
            "latest": (svc.get("latest") or "").strip(),
            "status": svc.get("status", ""),
            "eligible": svc.get("managed_update_eligible", True),
        }
    return out


def load_display_name_overrides() -> dict[str, str]:
    """Map {compose-basename: vps.display-name label value} gathered
    from all running containers. Lets a compose file override the
    Gatus card title (line 1) without touching Ansible config.

    Dokploy names containers `<compose-name>-<6-char-hash>-app-<N>`;
    we strip the suffix so spec entries can key on the stable
    compose-name (e.g., `vps-docs`). Client apps match on their
    Dokploy appName directly."""
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format",
             '{{.Names}}|{{.Label "vps.display-name"}}'],
            timeout=5, text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    m: dict[str, str] = {}
    suffix_re = re.compile(r"-[a-z0-9]{6}-app-\d+$")
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, label = line.split("|", 1)
        label = label.strip()
        if not label or label == "<no value>":
            continue
        base = suffix_re.sub("", name.strip())
        m[base] = label
    return m


def _image_shortname(image_repo: str) -> str:
    """`cloudflare/cloudflared` -> `cloudflared`, `twinproduction/gatus`
    -> `gatus`, `postgres` -> `postgres`, etc. Keeps the Gatus card title
    compact; the full repo is still visible via version-check.json."""
    if not image_repo:
        return ""
    return image_repo.rsplit("/", 1)[-1]


def _label_with_version(label: str, ver: dict | None,
                        display_override: str | None = None) -> str:
    """Render the Gatus endpoint title (line 1 of the card). Format
    when version info is available:
      - up-to-date:   `<display> <current>`
      - outdated:     `<display> <current> (<latest>)`
      - not-eligible: `<display> <<current>>`  (angle brackets; see
                       gatus-base.yaml.j2 dashboard-subheading).

    Display-name precedence (first non-empty wins):
      1. `display_override` -- from a `vps.display-name=...` compose label
         on the running container. Escape hatch for cases where the
         runtime image doesn't reflect the app's identity (e.g., nginx
         serving MkDocs HTML -> override to `mkdocs-material`).
      2. `ver.display_name` -- version-check.json's project name
         (`Keycloak`, `Traefik (Dokploy)`, ...), lowercased + paren
         suffix stripped. Fixes generic image last-segments
         automatically for tracked services.
      3. Image last-path segment (`cloudflare/cloudflared` -> `cloudflared`).
      4. Caller-provided `label` (hostname) as final fallback.
    """
    if not ver:
        return display_override or label
    display = (display_override or
               ver.get("display_name") or
               _image_shortname(ver.get("image") or ""))
    current = ver.get("current") or ""
    latest = ver.get("latest") or ""
    status = ver.get("status", "")
    eligible = ver.get("eligible", True)
    if not display and not current:
        return label
    not_eligible = status == "not-eligible" or eligible is False
    if not_eligible:
        core = f"{display} <{current}>".strip() if current else f"{display} <?>"
    elif status == "outdated" and latest and latest != "--" and current != latest:
        core = f"{display} {current} ({latest})".strip()
    else:
        core = f"{display} {current}".strip() if current else display
    return core


def build_infra_doc(spec_path: Path, version_map: dict[str, dict],
                    display_overrides: dict[str, str] | None = None,
                    slug_sink: set | None = None) -> str:
    """Render 40-infra.yaml from gatus-infra-spec.json. Each entry's
    `version_key` is looked up in version_map; if found, the version is
    appended to the endpoint NAME so it shows on the Gatus dashboard
    card. Specs with `version_key: null` get no annotation."""
    header = (
        "# Auto-generated by gatus-sync -- do not edit by hand.\n"
        "# Source spec: gatus-infra-spec.json (Ansible-rendered).\n"
        "# Versions appended to endpoint NAME from version-check.json.\n"
        "endpoints:\n"
    )
    if not spec_path.exists():
        return header + "  []  # gatus-infra-spec.json missing\n"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"gatus-sync: cannot read {spec_path}: {e}", file=sys.stderr)
        return header + "  []  # spec unreadable\n"

    parts = [header]
    for ep in spec:
        label = ep.get("label", "?")
        ver = version_map.get(ep.get("version_key") or "")
        # Compose-label override (vps.display-name). Spec entry carries
        # `compose_name` to identify the target container; when set,
        # a matching Docker label on that container wins over
        # auto-derivation. Enables per-app override without Ansible
        # config changes -- see apps/docs/wiki/src/how-to-deploy-apps.
        cn = ep.get("compose_name")
        override = (display_overrides or {}).get(cn) if cn else None
        name = _label_with_version(label, ver, display_override=override)
        url = ep.get("url", "")
        group = ep.get("group", "Infrastructure")
        accepted = ep.get("accepted") or [200]
        ignore_redirect = bool(ep.get("ignore_redirect"))
        interval = ep.get("interval", "60s")
        slug = ep.get("alert_slug", _slug(label))
        if slug_sink is not None:
            slug_sink.add(slug)
        cond = (
            f"[STATUS] == {accepted[0]}"
            if len(accepted) == 1
            else f"[STATUS] == any({', '.join(str(c) for c in accepted)})"
        )
        client_block = "    client:\n      ignore-redirect: true\n" if ignore_redirect else ""
        # Extra conditions let specs layer non-status checks on top of the
        # baseline [STATUS] == X -- e.g. backup-stats.json has status=="success".
        # See roles/infrastructure/templates/gatus-infra-spec.json.j2 usage.
        extra_conds = ep.get("extra_conditions") or []
        conds_block = f'      - "{cond}"\n' + "".join(
            f'      - "{c}"\n' for c in extra_conds
        )
        parts.append(
            f'  - name: "{name}"\n'
            f"    group: {group}\n"
            f'    url: "{url}"\n'
            f"    interval: {interval}\n"
            f"{client_block}"
            f"    conditions:\n"
            f"{conds_block}"
            f"    alerts:\n"
            f"      - type: custom\n"
            f'        description: "{slug}"\n'
        )
    return "".join(parts)


def build_doc(projects, api_base, api_key, infra_project, skip_names,
              auth_hostname, version_map=None, display_overrides=None,
              slug_sink: set | None = None):
    """Two endpoints per gated Dokploy app: internal + public. Group is
    the Dokploy project name so the notification title reads as
    "Gatus: <project> / <hostname> (<appName>)" -- enough context to
    identify the failing service without opening the status page."""
    parts = [
        "# Auto-generated by gatus-sync -- do not edit by hand.\n"
        "# Operator additions: drop a 99-<name>.yaml file in the same dir.\n"
        "endpoints:\n"
    ]
    seen = []  # (project, kind, name, domains, decision)
    wrote_any = False

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
        for kind, items in (("application", env.get("applications", []) or []),
                            ("compose", env.get("compose", []) or [])):
            for item in items:
                name = item.get("appName") or item.get("name") or "<noname>"
                if name in skip_names:
                    seen.append((pname, kind, name, [], "skip: infra-list"))
                    continue
                # Dokploy state gate. composeStatus / applicationStatus
                # enum = idle | running | done | error. 'done' means
                # successfully deployed and expected to be live -- the
                # only state where a Gatus endpoint (and its attached
                # Healthchecks alert) is meaningful. 'idle' covers apps
                # that are configured in Dokploy (domain registered,
                # Traefik route may even resolve) but never deployed;
                # monitoring them produces noise (false-green from the
                # oauth2-proxy redirect, or false-red from connection
                # errors) with no actionable meaning. 'error' means the
                # operator already knows something broke; 'running'
                # means a deploy is in-flight. Skip all three.
                status = (item.get("composeStatus")
                          or item.get("applicationStatus")
                          or "").lower()
                if status != "done":
                    seen.append((pname, kind, name, [],
                                 f"skip: status={status or 'none'}"))
                    continue
                domains = _fetch_domains(api_base, api_key, item, kind)
                hosts = [d for d in domains if d.get("host")
                         and d.get("host") != auth_hostname]
                if not hosts:
                    seen.append((pname, kind, name, [], "skip: no-domains"))
                    continue
                # Look up client-app version under the appName / repo
                # display key in version-check.json. Client services
                # are listed there as "<repo/repo>" (see
                # version-check.py.j2 enumerate_client_services).
                ver = None
                if version_map:
                    for k, v in version_map.items():
                        if k.lower().endswith("/" + name.lower()) or k.lower() == name.lower():
                            ver = v
                            break
                # ONE endpoint per app: prefer the public probe (end-to-end
                # signal through CF tunnel + Traefik + oauth2-proxy +
                # Keycloak). Title is the public domain; internal alias is
                # a fallback only when the app has no public domain
                # (shouldn't happen here since we skip no-domain apps
                # above, but defensive).
                host = hosts[0]["host"]
                # Client app can override via `vps.display-name` compose
                # label -- keyed by Dokploy appName.
                client_override = (display_overrides or {}).get(name)
                host_slug = _slug(host)
                if slug_sink is not None:
                    slug_sink.add(host_slug)
                parts.append(_endpoint_yaml(
                    name=_label_with_version(host, ver, display_override=client_override),
                    group=pname,
                    url=f"https://{host}",
                    accepted=[302],
                    description=host_slug,
                ))
                seen.append((pname, kind, name, [d.get("host") for d in hosts],
                             "wrote:1"))
                wrote_any = True

    if not wrote_any:
        parts.append("  []  # no gated Dokploy apps with domains\n")

    print(f"gatus-sync: examined {len(seen)} items:")
    for row in seen:
        print(f"  {row[4]:20s} project={row[0]!r:20s} kind={row[1]:11s} "
              f"name={row[2]!r:20s} domains={row[3]}")
    return "".join(parts)


def find_gatus_container(name_re: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"],
            timeout=5, text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"gatus-sync: couldn't list docker containers: {e}",
              file=sys.stderr)
        return None
    pattern = re.compile(name_re)
    for line in out.splitlines():
        if pattern.match(line.strip()):
            return line.strip()
    return None


def restart_gatus(container: str) -> None:
    print(f"gatus-sync: restarting {container} to pick up new endpoints")
    try:
        subprocess.check_call(["docker", "restart", container], timeout=30)
    except subprocess.SubprocessError as e:
        print(f"gatus-sync: restart failed: {e}", file=sys.stderr)


def write_homepage_summary(api_url, out_path):
    """Query Gatus /api/v1/endpoints/statuses and write a compact JSON summary
    the Homepage customapi widget consumes. Best-effort: any HTTP/JSON error
    leaves the previous summary in place (stale data beats no data on a
    dashboard)."""
    import datetime
    try:
        req = urllib.request.Request(
            f"{api_url.rstrip('/')}/api/v1/endpoints/statuses",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            endpoints = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as e:
        print(f"gatus-sync: homepage summary fetch failed: {e}", file=sys.stderr)
        return

    total = len(endpoints)
    down_items = []
    for ep in endpoints:
        results = ep.get("results") or []
        if not results:
            continue
        latest = results[0]
        if latest.get("success") is False:
            down_items.append({
                "name": ep.get("name") or ep.get("key") or "?",
                "group": ep.get("group") or "",
                "since": latest.get("timestamp") or "",
            })
    down = len(down_items)
    up = total - down
    oldest_failure_at = ""
    if down_items:
        oldest_failure_at = min((d["since"] for d in down_items if d["since"]), default="")

    # Prefix with emoji so the Homepage customapi widget (no native
    # coloring for list-type displays in v1.x) still communicates state
    # at a glance. Green/red circles are unicode -- render fine in every
    # browser + don't require Homepage icon theming. Partial-down edge
    # case (e.g. `total > 0 and down == 0 and up < total` -- deleted
    # endpoints mid-poll) falls into "All up" bucket for simplicity.
    if down == 0:
        status_text = f"\U0001F7E2 All {total} up"
    else:
        names = ", ".join(d["name"] for d in down_items[:3])
        if down > 3:
            names += f" +{down - 3} more"
        status_text = f"\U0001F534 DOWN: {names}"

    summary = {
        "total": total,
        "up": up,
        "down": down,
        "status_text": status_text,
        "oldest_failure_at": oldest_failure_at,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        tmp.replace(out_path)
        print(f"gatus-sync: wrote {out_path} ({up}/{total} up)")
    except OSError as e:
        print(f"gatus-sync: homepage summary write failed: {e}", file=sys.stderr)


def write_healthchecks_summary(api_url, api_key, out_path):
    """Query Healthchecks /api/v3/checks/ and emit a compact summary the
    Homepage customapi widget consumes. Same best-effort posture + same
    🔴/🟢 prefix convention as write_homepage_summary() (Gatus sibling).

    Healthchecks status strings (from api/v3 docs):
      up      -- last ping within expected interval + grace
      down    -- past expected interval + grace
      grace   -- past expected but still in grace window
      paused  -- operator-disabled
      new     -- never received a ping yet

    We count "down" as the alert state; "grace" / "new" / "paused" are
    neutral. Coloring flips red only when down > 0."""
    import datetime
    if not api_url or not api_key:
        return
    try:
        req = urllib.request.Request(
            api_url,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as e:
        print(f"gatus-sync: healthchecks summary fetch failed: {e}", file=sys.stderr)
        return

    checks = data.get("checks") or []
    total = len(checks)
    down_names = []
    up = 0
    paused = 0
    for c in checks:
        st = (c.get("status") or "").lower()
        if st == "up":
            up += 1
        elif st == "down":
            down_names.append(c.get("name") or "?")
        elif st == "paused":
            paused += 1
    down = len(down_names)

    if down == 0:
        status_text = f"\U0001F7E2 All {up}/{total} up"
    else:
        preview = ", ".join(down_names[:3])
        if down > 3:
            preview += f" +{down - 3} more"
        status_text = f"\U0001F534 DOWN: {preview}"

    summary = {
        "total": total,
        "up": up,
        "down": down,
        "paused": paused,
        "status_text": status_text,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        tmp.replace(out_path)
        print(f"gatus-sync: wrote {out_path} ({up}/{total} up, {down} down)")
    except OSError as e:
        print(f"gatus-sync: healthchecks summary write failed: {e}", file=sys.stderr)


# ─── Healthchecks orphan-check cleanup ─────────────────────────────────────
# Gatus's custom alert URL uses `?create=1`, so Healthchecks auto-creates a
# `gatus-<slug>` check on first failure ping. When an endpoint disappears
# from Gatus (Dokploy project torn down, compose renamed, host changed),
# the Healthchecks check orphans and eventually pages on dead-man timeout.
#
# This pass runs at the end of every gatus-sync: for each HC check named
# `gatus-*` that isn't in the current expected-slug set, pause it. Pause
# (not delete) preserves history and auto-resumes on the next ping if the
# endpoint comes back. Operator can delete paused checks manually from
# the HC UI once sure.


def _classify_orphans(hc_checks: list[dict], expected_slugs: set[str]) -> list[dict]:
    """Pure helper -- returns the subset of HC checks that should be
    paused: name starts with 'gatus-', slug not in expected set, and
    not already paused. Exposed separately for unit testing."""
    expected_names = {f"gatus-{s}" for s in expected_slugs}
    to_pause: list[dict] = []
    for check in hc_checks:
        name = (check.get("name") or "").strip()
        if not name.startswith("gatus-"):
            continue
        if name in expected_names:
            continue
        status = (check.get("status") or "").lower()
        if status == "paused":
            continue
        to_pause.append(check)
    return to_pause


def _hc_check_uuid(check: dict) -> str | None:
    """HC API v3 exposes check UUIDs in the per-check URLs (`ping_url`,
    `update_url`). The `uuid` field is not always present on list
    responses -- parse from the tail of the ping URL when needed."""
    u = check.get("uuid")
    if u:
        return u
    for k in ("update_url", "ping_url"):
        v = check.get(k) or ""
        if "/" in v:
            tail = v.rstrip("/").rsplit("/", 1)[-1]
            if tail and tail != "ping":
                return tail
    return None


def prune_orphan_hc_checks(api_list_url: str, api_key_rw: str,
                           expected_slugs: set[str]) -> None:
    """Enumerate HC checks via GET <api_list_url>, filter to orphans via
    `_classify_orphans`, and POST pause for each. Best-effort: any HTTP
    / JSON failure logs + returns (the worst case is a stale orphan,
    not a false pause). Idempotent -- already-paused checks are skipped."""
    if not api_list_url or not api_key_rw:
        return
    try:
        req = urllib.request.Request(
            api_list_url,
            headers={"X-Api-Key": api_key_rw, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as e:
        print(f"gatus-sync: HC orphan enumeration failed: {e}", file=sys.stderr)
        return

    checks = data.get("checks") or []
    to_pause = _classify_orphans(checks, expected_slugs)
    if not to_pause:
        return

    # Base = list URL with the trailing `checks/` kept as the parent path.
    # e.g. http://hc/api/v3/checks/ + <uuid>/pause/ -> /api/v3/checks/<uuid>/pause/
    base = api_list_url if api_list_url.endswith("/") else api_list_url + "/"
    for check in to_pause:
        uuid = _hc_check_uuid(check)
        name = check.get("name") or "?"
        if not uuid:
            print(f"gatus-sync: cannot resolve UUID for orphan {name!r}; skipping",
                  file=sys.stderr)
            continue
        pause_url = f"{base}{uuid}/pause/"
        try:
            preq = urllib.request.Request(
                pause_url,
                headers={"X-Api-Key": api_key_rw, "Accept": "application/json"},
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(preq, timeout=10) as presp:
                presp.read()
            print(f"gatus-sync: paused orphan HC check {name!r} ({uuid})")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"gatus-sync: pause failed for {name!r}: {e}", file=sys.stderr)


def main():
    api_base = _env("DOKPLOY_API_BASE")
    api_key = _env("DOKPLOY_API_KEY")
    infra_project = _env("DOKPLOY_INFRA_PROJECT")
    out_path = Path(_env("GATUS_CONFIG_PATH"))
    container_re = _env("GATUS_CONTAINER_NAME_RE")
    skip_names = {
        n.strip() for n in _env("INFRA_COMPOSE_NAMES").split(",") if n.strip()
    }
    auth_hostname = _env("AUTH_HOSTNAME")
    summary_path_str = os.environ.get("GATUS_SUMMARY_PATH", "").strip()
    gatus_api_url = os.environ.get("GATUS_API_URL", "").strip()

    # Load version map up-front; reused by both infra + client renders.
    version_check_str = os.environ.get("VERSION_CHECK_PATH", "").strip()
    version_map: dict[str, dict] = {}
    if version_check_str:
        version_map = load_version_map(Path(version_check_str))
    # `vps.display-name` compose-label overrides (container-scoped).
    display_overrides = load_display_name_overrides()

    # Accumulate every slug emitted this run (infra + dokploy apps); used
    # by the HC orphan-pause pass at the end of main() to identify
    # `gatus-<slug>` HC checks whose endpoint no longer exists.
    expected_slugs: set[str] = set()

    projects = dokploy_get(api_base, "/project.all", api_key)
    new_yaml = build_doc(projects, api_base, api_key, infra_project,
                         skip_names, auth_hostname, version_map=version_map,
                         display_overrides=display_overrides,
                         slug_sink=expected_slugs)

    config_changed = not (
        out_path.exists()
        and out_path.read_text(encoding="utf-8") == new_yaml
    )

    # Render the infra endpoint file from the JSON spec.
    infra_spec_str = os.environ.get("GATUS_INFRA_SPEC_PATH", "").strip()
    infra_out_str = os.environ.get("GATUS_INFRA_CONFIG_PATH", "").strip()
    infra_changed = False
    if infra_spec_str and infra_out_str:
        infra_spec_path = Path(infra_spec_str)
        infra_out_path = Path(infra_out_str)
        infra_yaml = build_infra_doc(infra_spec_path, version_map,
                                     display_overrides=display_overrides,
                                     slug_sink=expected_slugs)
        infra_changed = not (
            infra_out_path.exists()
            and infra_out_path.read_text(encoding="utf-8") == infra_yaml
        )
        if infra_changed:
            infra_out_path.parent.mkdir(parents=True, exist_ok=True)
            infra_out_path.write_text(infra_yaml, encoding="utf-8")
            print(f"gatus-sync: wrote {infra_out_path}")

    if config_changed or infra_changed:
        if config_changed:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(new_yaml, encoding="utf-8")
            print(f"gatus-sync: wrote {out_path}")

        container = find_gatus_container(container_re)
        if container:
            restart_gatus(container)
        else:
            print("gatus-sync: no running Gatus container matched "
                  f"{container_re!r} -- config written, will apply next Gatus start",
                  file=sys.stderr)
    else:
        print("gatus-sync: no change")

    # Summary writes are best-effort and always run -- refresh the
    # dashboard tick even when the endpoint list is stable.
    if summary_path_str and gatus_api_url:
        write_homepage_summary(gatus_api_url, Path(summary_path_str))

    hc_path = os.environ.get("HEALTHCHECKS_SUMMARY_PATH", "").strip()
    hc_url = os.environ.get("HEALTHCHECKS_API_URL", "").strip()
    hc_key = os.environ.get("HEALTHCHECKS_API_KEY", "").strip()
    if hc_path and hc_url and hc_key:
        write_healthchecks_summary(hc_url, hc_key, Path(hc_path))

    # Orphan-check cleanup: uses the RW key (readonly can't pause). Silent
    # no-op when either is unset (upgrade path -- operators on older vaults
    # won't have the RW key yet).
    hc_key_rw = os.environ.get("HEALTHCHECKS_API_KEY_RW", "").strip()
    if hc_url and hc_key_rw:
        prune_orphan_hc_checks(hc_url, hc_key_rw, expected_slugs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
