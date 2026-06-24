#!/usr/bin/env python3
"""Auto-detecting version + CVE-visibility check (Community).

Emit a JSON report of every running service's pinned version vs. the
latest released upstream. Consumed by the Homepage "Versions" widget via
gatus-sync (which reads /var/lib/catena/version-check.json) and by the
operator-side cve-upgrade-report aggregator.

Detection is AUTOMATIC: there is no service registry to maintain. The
check enumerates every running container (docker ps) and swarm service
(docker service ls), derives each service's upstream straight from its
image reference (Docker Hub official/org, GHCR -> GitHub, Quay), and
compares the running tag against the newest matching upstream tag. New
apps appear the moment they run; nothing to add.

For the rare image whose upstream can't be derived from the ref (private
registry, a repo whose releases live somewhere other than the registry),
drop an entry in the optional overrides file (VERSION_CHECK_OVERRIDES,
default /etc/catena/version-check-overrides.json) to set the source /
repo / tag pattern / display name, or to exclude it.

This ships in catena-ce: a self-hosted Community deployment sees its own
version freshness without a license. When the Business managed-update
engine is licensed it ALSO writes its updater-state files, which this
script merges into the report (the summary card) -- absent on CE, so the
card degrades to a plain version list.

Stdlib-only. Network calls are best-effort: a registry/API hiccup yields
a blank "latest" (status "unknown") rather than a hard failure.

Output shape (per service row, consumed by gatus-sync):
  {"name", "current", "latest", "up_to_date", "status",
   "display_label", "display_state", "upstream_url", "full_image",
   "managed_update_eligible"}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 10
OUTPUT_PATH = os.environ.get(
    "VERSION_CHECK_OUTPUT", "/var/lib/catena/version-check.json"
)
OVERRIDES_FILE = os.environ.get(
    "VERSION_CHECK_OVERRIDES", "/etc/catena/version-check-overrides.json"
)

# Business managed-bump engine state -- read-only merge into the report.
# Absent on CE (-> empty updater_state); the licensed engine writes these.
MANAGED_STATE_FILE = os.environ.get(
    "MANAGED_VERSIONS_STATE_FILE", "/var/lib/catena/managed-versions.json"
)
MANAGED_FAILED_FILE = os.environ.get(
    "MANAGED_VERSIONS_FAILED_FILE",
    "/var/lib/catena/managed-versions.failed.json",
)
MANAGED_PAUSE_FLAG = os.environ.get(
    "MANAGED_UPDATE_PAUSE_FLAG", "/etc/catena/stack-update.disabled"
)

_FULL_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[.-][\w.-]+)?$")
_PARTIAL_SEMVER_RE = re.compile(r"^v?\d+(?:\.\d+)?(?:[.-][\w.-]+)?$")
_FLOATING_TAGS = frozenset({"latest", "stable", "alpine", "edge", "main", "master", ""})


def log(msg: str) -> None:
    print(f"[version-check] {msg}", flush=True)


def classify_tag(tag: str) -> str:
    if tag is None:
        return "unset"
    t = tag.strip()
    if not t:
        return "unset"
    if t.lower() in _FLOATING_TAGS:
        return "floating"
    if _FULL_SEMVER_RE.match(t):
        return "full_semver"
    if _PARTIAL_SEMVER_RE.match(t):
        return "partial"
    return "floating"


# ─── docker discovery ────────────────────────────────────────────────────


def _docker(*args: str) -> str:
    try:
        r = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ""


# Swarm task containers are named "<service>.<slot>.<id>"; their image is
# already covered by the `docker service ls` row, so skip them in `ps`.
_SWARM_TASK_RE = re.compile(r"^.+\.\d+\.[a-z0-9]+$")
# Dokploy app containers: "<app>-<6hex>-app-<n>" -> stable base "<app>".
_DOKPLOY_SUFFIX_RE = re.compile(r"-[a-z0-9]{6}-app-\d+$")


def discover_running_services() -> list[dict]:
    """Every distinct running image, from swarm services + plain
    containers. Returns [{name, image, display_override}] deduped by image
    reference (replicas collapse to one row)."""
    found: dict[str, dict] = {}

    # Swarm services first (authoritative for replicated infra like dokploy).
    for line in _docker("service", "ls", "--format", "{{.Name}}|{{.Image}}").splitlines():
        name, _, image = line.partition("|")
        image = image.strip()
        if not image:
            continue
        found.setdefault(image, {"name": name.strip(), "image": image,
                                 "display_override": ""})

    # Plain containers, skipping swarm tasks (covered above).
    ps = _docker("ps", "--format",
                 '{{.Names}}|{{.Image}}|{{.Label "vps.display-name"}}')
    for line in ps.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, image = parts[0].strip(), parts[1].strip()
        label = parts[2].strip() if len(parts) > 2 else ""
        if not image or _SWARM_TASK_RE.match(name):
            continue
        base = _DOKPLOY_SUFFIX_RE.sub("", name)
        row = found.setdefault(image, {"name": base, "image": image,
                                       "display_override": ""})
        if label and label != "<no value>" and not row["display_override"]:
            row["display_override"] = label
    return list(found.values())


# ─── image-ref parsing + upstream derivation ─────────────────────────────


def parse_image(image: str) -> tuple[str, str, str]:
    """`quay.io/keycloak/keycloak:26@sha256:..` -> (registry, repo, tag).

    No-registry single-segment images are Docker Hub official images, so
    the repo is normalised to `library/<name>` (what the Hub API wants)."""
    ref = image.split("@", 1)[0]
    first, slash, rest = ref.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", ref
    if ":" in path.rsplit("/", 1)[-1]:
        repo, _, tag = path.rpartition(":")
    else:
        repo, tag = path, ""
    if registry == "docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    return registry, repo, tag


def tag_pattern_for(tag: str) -> str:
    """Build a regex that matches upstream tags of the SAME shape as the
    running tag, so a `16-alpine` pin compares against other `*-alpine`
    tags and a bare `1.2.3` against bare semver. Returns "" for a tag we
    should not compare (floating)."""
    cls = classify_tag(tag)
    if cls not in ("full_semver", "partial"):
        return ""
    suffix = ""
    m = re.match(r"^v?\d+(?:\.\d+)*([.-][\w.-]+)?$", tag)
    if m and m.group(1):
        suffix = re.escape(m.group(1))
    return rf"^v?\d+(?:\.\d+)+{suffix}$"


def derive_upstream(registry: str, repo: str) -> dict | None:
    """Map a parsed image to where its latest version is published.
    None when the registry is one we can't query (private/unknown)."""
    if registry == "ghcr.io":
        return {"source": "github", "repo": repo,
                "url": f"https://github.com/{repo}/releases"}
    if registry == "quay.io":
        return {"source": "quay", "repo": repo,
                "url": f"https://quay.io/repository/{repo}?tab=tags"}
    if registry == "docker.io":
        if repo.startswith("library/"):
            url = f"https://hub.docker.com/_/{repo.split('/', 1)[1]}/tags"
        else:
            url = f"https://hub.docker.com/r/{repo}/tags"
        return {"source": "dockerhub", "repo": repo, "url": url}
    return None


# ─── upstream queries ────────────────────────────────────────────────────


def _http_json(url: str, headers: dict | None = None):
    try:
        req = urllib.request.Request(url, headers=headers or {
            "User-Agent": "catena-version-check",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
        log(f"fetch failed: {url} -> {e}")
        return None


def _tag_semver_key(tag: str) -> tuple:
    m = re.match(r"^v?([0-9]+(?:\.[0-9]+)*)", tag)
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split("."))


def github_latest_release(repo: str) -> str:
    data = _http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if not isinstance(data, dict):
        return ""
    tag = data.get("tag_name", "")
    if tag.startswith("version/"):
        tag = tag.split("/", 1)[1]
    return tag


def _highest_matching(names: list[str], pattern: str) -> str:
    rx = re.compile(pattern)
    matches = [n for n in names if n and rx.fullmatch(n)]
    if not matches:
        return ""
    matches.sort(key=_tag_semver_key, reverse=True)
    return matches[0]


def dockerhub_latest_matching(repo: str, pattern: str) -> str:
    url = (f"https://hub.docker.com/v2/repositories/{repo}/tags"
           f"?page_size=200&ordering=last_updated")
    data = _http_json(url)
    if not isinstance(data, dict):
        return ""
    return _highest_matching(
        [t.get("name", "") for t in (data.get("results", []) or [])], pattern)


def quay_latest_matching(repo: str, pattern: str) -> str:
    url = f"https://quay.io/api/v1/repository/{repo}/tag/?onlyActiveTags=true&limit=100"
    data = _http_json(url)
    if not isinstance(data, dict):
        return ""
    return _highest_matching(
        [t.get("name", "") for t in (data.get("tags", []) or [])], pattern)


def resolve_latest(up: dict, tag: str) -> str:
    if up["source"] == "github":
        return github_latest_release(up["repo"])
    pattern = up.get("tag_regex") or tag_pattern_for(tag)
    if not pattern:
        return ""
    if up["source"] == "dockerhub":
        return dockerhub_latest_matching(up["repo"], pattern)
    if up["source"] == "quay":
        return quay_latest_matching(up["repo"], pattern)
    return ""


def normalize(v: str) -> str:
    v = v.strip()
    return v[1:] if v.startswith("v") else v


def is_up_to_date(current: str, latest: str) -> bool:
    c, latest_n = normalize(current), normalize(latest)
    if not c or not latest_n:
        return False
    if c == latest_n:
        return True
    return latest_n.startswith(c + ".") or latest_n.startswith(c + "-")


# ─── overrides ───────────────────────────────────────────────────────────


def load_overrides(path: str) -> dict:
    """Optional {repo: {display_name, source, repo, tag_regex, upstream_url,
    exclude}} map for images whose upstream can't be auto-derived. Keyed by
    the parsed repo (e.g. "library/nextcloud", "keycloak/keycloak")."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _shortname(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


# ─── Business updater-state merge (absent on CE) ─────────────────────────


def _load_updater_state() -> dict:
    out = {
        "paused": False, "paused_since": None, "last_run": None,
        "last_bumped_count": 0, "last_rollback_count": 0,
        "quarantined_count": 0, "quarantined": [],
    }
    try:
        if os.path.exists(MANAGED_PAUSE_FLAG):
            out["paused"] = True
            try:
                out["paused_since"] = open(MANAGED_PAUSE_FLAG).read().strip() or None
            except OSError:
                pass
    except (OSError, ValueError):
        pass
    try:
        if os.path.exists(MANAGED_STATE_FILE):
            with open(MANAGED_STATE_FILE) as f:
                st = json.load(f)
            us = (st or {}).get("updater_state") or {}
            out["last_run"] = us.get("last_run")
            out["last_bumped_count"] = int(us.get("last_bumped_count") or 0)
            out["last_rollback_count"] = int(us.get("last_rollback_count") or 0)
            if us.get("paused") is not None:
                out["paused"] = bool(us["paused"])
            if us.get("paused_since"):
                out["paused_since"] = us["paused_since"]
    except (OSError, ValueError):
        pass
    try:
        if os.path.exists(MANAGED_FAILED_FILE):
            with open(MANAGED_FAILED_FILE) as f:
                q = json.load(f)
            if isinstance(q, dict):
                flat = [{"service": k, "versions": list(v)}
                        for k, v in q.items() if isinstance(v, list) and v]
                out["quarantined"] = flat
                out["quarantined_count"] = sum(len(e["versions"]) for e in flat)
    except (OSError, ValueError):
        pass
    return out


# ─── report ──────────────────────────────────────────────────────────────


def build_service_row(svc: dict, overrides: dict) -> dict:
    image = svc["image"]
    registry, repo, tag = parse_image(image)
    ov = overrides.get(repo, {})
    if ov.get("exclude"):
        return {}

    up = derive_upstream(registry, repo)
    if ov.get("source") and ov.get("repo"):
        up = {"source": ov["source"], "repo": ov["repo"],
              "url": ov.get("upstream_url", "")}
    if up and ov.get("tag_regex"):
        up["tag_regex"] = ov["tag_regex"]

    display = (ov.get("display_name") or svc.get("display_override")
               or _shortname(repo))
    upstream_url = (ov.get("upstream_url") or (up or {}).get("url", ""))
    eligible = classify_tag(tag) == "full_semver"

    latest = resolve_latest(up, tag) if up else ""
    if not tag or classify_tag(tag) == "floating":
        status, up_to_date = "floating", None
        display_label = f"{tag or ':latest'} (not pinned)"
        display_state = "info"
    elif not latest:
        status, up_to_date = "unknown", None
        display_label = tag
        display_state = "info"
    else:
        up_to_date = is_up_to_date(tag, latest)
        status = "up-to-date" if up_to_date else "outdated"
        display_label = f"{tag} ✓" if up_to_date else f"{tag} → {latest}"
        display_state = "success" if up_to_date else "warning"

    return {
        "name": display,
        "current": tag or "(none)",
        "latest": latest or "unknown",
        "up_to_date": up_to_date,
        "status": status,
        "display_label": display_label,
        "display_state": display_state,
        "upstream_url": upstream_url,
        "full_image": image,
        "managed_update_eligible": eligible,
    }


def build_report() -> dict:
    overrides = load_overrides(OVERRIDES_FILE)
    services = []
    for svc in sorted(discover_running_services(), key=lambda s: s["image"]):
        row = build_service_row(svc, overrides)
        if row:
            services.append(row)

    outdated = sum(1 for s in services if s["status"] == "outdated")
    top = next((s for s in services if s["status"] == "outdated"), None)
    if outdated:
        summary_line = f"{outdated} service(s) need bumping"
    elif services and any(s["status"] != "unknown" for s in services):
        summary_line = "All pinned services up-to-date"
    else:
        summary_line = "Check inconclusive (some upstreams unreachable)"

    updater_state = _load_updater_state()
    if updater_state["paused"]:
        updater_line = ("⏸ Managed updates PAUSED"
                        + (f" since {updater_state['paused_since']}"
                           if updater_state["paused_since"] else ""))
    elif updater_state["last_run"]:
        updater_line = (
            f"Managed updates ENABLED · last run {updater_state['last_run']}"
            f" · {updater_state['last_bumped_count']} bumped"
            f", {updater_state['last_rollback_count']} rollbacks"
            + (f", {updater_state['quarantined_count']} quarantined"
               if updater_state["quarantined_count"] else ""))
    else:
        updater_line = "Managed updates not licensed (Community)"

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outdated_count": outdated,
        "summary": summary_line,
        "top_outdated_name": top["name"] if top else "",
        "top_outdated_current": top["current"] if top else "",
        "top_outdated_latest": top["latest"] if top else "",
        "services": services,
        "updater_state": updater_state,
        "updater_summary": updater_line,
    }


def main() -> int:
    report = build_report()
    if "--print" in sys.argv:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    os.chmod(OUTPUT_PATH, 0o644)
    log(f"wrote {OUTPUT_PATH} ({report['outdated_count']} outdated of "
        f"{len(report['services'])} services)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
