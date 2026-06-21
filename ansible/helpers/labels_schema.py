"""Compose-label parsing + image-tag classification for the vps.* vocabulary.

Canonical Python source of truth for the `vps.*` compose-label vocabulary
on the host side. Installed flat (as labels_schema.py) alongside the host
scripts that import it -- the public-port reconciler
(scripts/catena-public-ports.py) and the dashboard/access provisioners --
so siblings can `from labels_schema import slugify,
extract_vps_auth_labels, resolve_auth_mode`.

The catena-admin Go shell carries its own implementation of the same
vocabulary (internal/admin/labels); the two must stay in lockstep on the
`vps.*` grammar.

Stdlib-only by design so it installs beside the host scripts without
dragging any deps.
"""

from __future__ import annotations

import re

# ─── Slugification ────────────────────────────────────────────────────────
#
# Used for both Traefik router/service names AND Dokploy compose alias
# matching. Lowercase + non-[a-z0-9] -> "-", strip leading/trailing dashes.

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(s: str) -> str:
    return _SLUG_RE.sub("-", str(s).lower()).strip("-")


# ─── vps.auth.* label extraction ──────────────────────────────────────────
#
# Per-app gating intent is declared via compose labels. The label vocabulary
# is documented in docs/src/content/docs/en/how-to-deploy-apps.md:
#   vps.auth.mode               public | admin-only | private (default)
#   vps.auth.groups             comma-separated group names
#   vps.auth.protected          true|false -- when true, exposing the app to
#                               `visitor` (public) is flagged by the catena-
#                               admin Access tab guardrail. For sensitive
#                               surfaces that must never go public.
#   vps.auth.oidc               true|false (additive on top of forward-auth)
#   vps.auth.oidc.redirect_uris csv URLs
#   vps.auth.oidc.scopes        space-separated scopes

_VPS_AUTH_LABEL_RE = re.compile(
    r"['\"]?vps\.auth\.(groups|mode|protected|oidc(?:\.redirect_uris|\.scopes)?)['\"]?"
    r"\s*[=:]\s*['\"]?([^'\"\n#]+?)['\"]?\s*(?:\n|$|#)",
    re.IGNORECASE,
)


def extract_vps_auth_labels(compose_text: str) -> dict:
    """Return a dict pulled from compose labels. Possible keys:
      - 'groups'             : list[str]
      - 'mode'               : str
      - 'protected'          : bool
      - 'oidc'               : bool
      - 'oidc_redirect_uris' : list[str]
      - 'oidc_scopes'        : list[str]
    Empty dict if no recognized labels are present."""
    if not compose_text:
        return {}
    labels: dict = {}
    for m in _VPS_AUTH_LABEL_RE.finditer(compose_text):
        key = m.group(1).lower()
        val = m.group(2).strip()
        if key == "groups":
            labels["groups"] = [g.strip() for g in val.split(",") if g.strip()]
        elif key == "mode":
            labels["mode"] = val.lower()
        elif key == "protected":
            labels["protected"] = val.lower() in ("true", "yes", "1", "on")
        elif key == "oidc":
            labels["oidc"] = val.lower() in ("true", "yes", "1", "on")
        elif key == "oidc.redirect_uris":
            labels["oidc_redirect_uris"] = [
                u.strip() for u in val.split(",") if u.strip()
            ]
        elif key == "oidc.scopes":
            labels["oidc_scopes"] = [
                s.strip() for s in val.split() if s.strip()
            ]
    return labels


# ─── dokploy-network service-alias extraction ─────────────────────────────
#
# dashboard-sync routes each domain's Traefik backend to the dokploy-network
# alias of the compose service that domain fronts (the Dokploy domain record's
# serviceName). A multi-domain app (Nextcloud + Talk HPB) gives each service
# its own alias (`nextcloud`, `signaling`), so the router must target the
# service-specific alias, not blindly the appName slug. This is the host-side
# resolver gate_routes consults. Stdlib-only (no YAML dep): an indentation
# parser over the standard 2-space compose shape.


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def extract_service_aliases(compose_text: str) -> dict:
    """Return {service_name: [dokploy-network aliases]} parsed from a compose
    body. Only dokploy-network aliases are collected (the handle Traefik
    resolves backends through); a service with none maps to []. Handles both
    the block list form

        services:
          app:
            networks:
              dokploy-network:
                aliases:
                  - nextcloud

    and the flow form (`aliases: [nextcloud]`). Best-effort: malformed
    indentation yields a partial map, never an exception. YAML-less so it runs
    beside dashboard-sync on a stdlib-only host."""
    out: dict = {}
    if not compose_text:
        return out
    stack: list[tuple[int, str]] = []  # (indent, key) path from the root
    for raw in compose_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind = _line_indent(raw)
        stripped = raw.strip()
        if stripped.startswith("- "):
            # List item: contributes only under services.<svc>.networks.
            # dokploy-network.aliases.
            path = [k for _, k in stack]
            if (
                len(path) >= 5
                and path[-1] == "aliases"
                and path[-2] == "dokploy-network"
                and path[-3] == "networks"
                and path[-5] == "services"
            ):
                out.setdefault(path[-4], []).append(
                    stripped[2:].strip().strip("\"'")
                )
            continue
        if ":" not in stripped:
            continue
        while stack and stack[-1][0] >= ind:
            stack.pop()
        key, _, val = stripped.partition(":")
        key = key.strip().strip("\"'")
        val = val.strip()
        stack.append((ind, key))
        path = [k for _, k in stack]
        # Register every service so callers can distinguish "no alias" from
        # "unknown service".
        if len(path) == 2 and path[-2] == "services":
            out.setdefault(key, [])
        # Flow-list aliases: dokploy-network: aliases: [a, b]
        if (
            key == "aliases"
            and val.startswith("[")
            and val.endswith("]")
            and len(path) >= 5
            and path[-2] == "dokploy-network"
            and path[-3] == "networks"
            and path[-5] == "services"
        ):
            out.setdefault(path[-4], []).extend(
                x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()
            )
    return out


# ─── vps.homepage.* label extraction ──────────────────────────────────────

_VPS_HOMEPAGE_LABEL_RE = re.compile(
    r"['\"]?vps\.homepage\.(name|icon|description|hidden)['\"]?"
    r"\s*[=:]\s*['\"]?([^'\"\n#]+?)['\"]?\s*(?:\n|$|#)",
    re.IGNORECASE,
)


def extract_vps_homepage_labels(compose_text: str) -> dict:
    """Return a dict with any subset of {'name', 'icon', 'description',
    'hidden'} pulled from vps.homepage.* compose labels. `hidden` is coerced
    to a bool; "true"/"yes"/"1"/"on" -> True, anything else -> False.
    Empty dict if no recognized labels are present."""
    if not compose_text:
        return {}
    out: dict = {}
    for m in _VPS_HOMEPAGE_LABEL_RE.finditer(compose_text):
        key = m.group(1).lower()
        val = m.group(2).strip()
        if key == "hidden":
            out["hidden"] = val.lower() in ("true", "yes", "1", "on")
        else:
            out[key] = val
    return out


# ─── vps.expose.* label extraction (public non-HTTP ports) ────────────────
#
# Direct public ports for protocols Cloudflare Tunnel cannot carry (SMTP,
# IMAPS, TURN media, ...). A Dokploy template declares the host ports it
# publishes via:
#   vps.expose.tcp   comma-separated ports / ranges (e.g. 25,465,587,993)
#   vps.expose.udp   comma-separated ports / ranges (e.g. 3478,5349,50000-50100)
#
# These feed the public-port registry (helpers/public_ports.py +
# vps-scripts/catena-public-ports.sh), which is the single source of truth
# for ufw rules, DOCKER-USER guards, the validate allowlist, the external
# nmap expectation, and the generated operator inventory. App-published
# ports default to scope `any` (reachable from the public internet) -- that
# is the only reason a template would declare them; tailnet/rfc1918-scoped
# ports are infra-owned and declared via the `public_ports` role var, not
# labels.

_VPS_EXPOSE_LABEL_RE = re.compile(
    r"['\"]?vps\.expose\.(tcp|udp)['\"]?"
    r"\s*[=:]\s*['\"]?([^'\"\n#]+?)['\"]?\s*(?:\n|$|#)",
    re.IGNORECASE,
)

_PORT_TOKEN_RE = re.compile(r"^(\d{1,5})(?:[-:](\d{1,5}))?$")


def _parse_port_token(tok: str) -> tuple[int, int] | None:
    """Parse a single port or `lo-hi` range token into a (lo, hi) tuple.

    Accepts `-` or `:` as the range separator (compose uses `:`; ufw uses
    `:`; humans write `-`). Returns None for malformed tokens or ports
    outside 1..65535 / inverted ranges, so a typo in one token never opens
    a wider range than intended."""
    m = _PORT_TOKEN_RE.match(tok.strip())
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) is not None else lo
    if not (1 <= lo <= 65535 and 1 <= hi <= 65535) or hi < lo:
        return None
    return (lo, hi)


def extract_vps_expose_labels(compose_text: str) -> dict:
    """Return {'tcp': [(lo, hi), ...], 'udp': [(lo, hi), ...]} from
    vps.expose.* compose labels. Each entry is a normalized (lo, hi) port
    range (a single port is (n, n)). Malformed tokens are dropped. Keys are
    present only when the corresponding label appears with >=1 valid token.
    Empty dict if no recognized labels are present."""
    if not compose_text:
        return {}
    out: dict = {}
    for m in _VPS_EXPOSE_LABEL_RE.finditer(compose_text):
        proto = m.group(1).lower()
        ranges: list[tuple[int, int]] = []
        for tok in m.group(2).split(","):
            tok = tok.strip()
            if not tok:
                continue
            parsed = _parse_port_token(tok)
            if parsed is not None:
                ranges.append(parsed)
        if ranges:
            out.setdefault(proto, []).extend(ranges)
    return out


# ─── Auth-mode resolution ─────────────────────────────────────────────────


def resolve_auth_mode(labels: dict, app_name: str = "") -> tuple[str, list[str], bool]:
    """Collapse vps.auth.mode + vps.auth.groups into
    (resolved_mode, allowed_groups, is_public) under DEFAULT-DENY.

    Resolution:
      - `visitor` in groups, OR mode=public -> PUBLIC: no proxy, no
        groups, is_public=True.
      - mode=admin-only                      -> allowed = ['admin'].
      - mode=private + groups                -> allowed = those groups
        + 'admin'. `private` here just documents intent; the listed
        groups are what gate.
      - mode=private, no groups              -> allowed = ['client',
        'staff', 'admin'] (the default authenticated audience).
      - explicit groups, mode unset/blank    -> allowed = those groups
        + 'admin' (the per-department default-deny path; identical to
        `private` + groups).
      - nothing (no mode, no groups)         -> DENY: allowed =
        ['admin'] only. The app is unreachable to every non-admin.
      - unknown mode                         -> DENY (secure default),
        with a warning.

    `admin` is ALWAYS present in a non-public allowed set -- the operator
    is a superuser and can never be locked out of an app by a label.

    The returned `resolved_mode` is normalized to one of:
      public | admin-only | restricted | deny
    (`private` collapses to `restricted`; both `deny` outcomes report
    "deny"). Callers badge/route off this. Warnings are exposed via the
    module-level `last_warnings()` helper; the function never raises."""
    raw_mode = (labels.get("mode") or "").strip().lower()
    groups = [g for g in (labels.get("groups") or []) if g]
    original_groups = list(groups)
    warnings: list[str] = []

    # 1. visitor keyword / explicit public -> public (no auth).
    if raw_mode == "public" or "visitor" in groups:
        if raw_mode == "public" and [g for g in original_groups if g != "visitor"]:
            warnings.append(
                f"[{app_name}]: vps.auth.mode=public ignores "
                f"vps.auth.groups={original_groups!r}; no gating will be "
                f"applied. Drop one of the labels to clarify."
            )
        if "visitor" in groups and len(set(groups)) > 1:
            warnings.append(
                f"[{app_name}]: vps.auth.groups={original_groups!r} mixes "
                f"`visitor` with other groups; `visitor` means public, so "
                f"the other groups are ignored. Drop `visitor` to gate."
            )
        resolved_mode, allowed, is_public = "public", [], True
    # 2. admin-only sugar.
    elif raw_mode == "admin-only":
        if original_groups and set(original_groups) != {"admin"}:
            warnings.append(
                f"[{app_name}]: vps.auth.mode=admin-only overrides "
                f"vps.auth.groups={original_groups!r}; using ['admin']. "
                f"Drop one of the labels to clarify."
            )
        resolved_mode, allowed, is_public = "admin-only", ["admin"], False
    # 3. private: gated to the listed groups; defaults to the broad
    #    client+staff audience when no groups are named.
    elif raw_mode == "private":
        if groups:
            allowed = sorted(set(groups) | {"admin"})
        else:
            allowed = ["admin", "client", "staff"]
        resolved_mode, is_public = "restricted", False
    # 4. explicit groups, no (or blank) mode -> per-group default-deny.
    elif groups:
        if raw_mode:
            warnings.append(
                f"[{app_name}]: unknown vps.auth.mode={raw_mode!r}; honoring "
                f"vps.auth.groups={original_groups!r}. Valid modes: public, "
                f"private, admin-only (or omit mode and list groups)."
            )
        allowed = sorted(set(groups) | {"admin"})
        resolved_mode, is_public = "restricted", False
    # 5. nothing declared (or unknown mode + no groups) -> DENY.
    else:
        if raw_mode:
            warnings.append(
                f"[{app_name}]: unknown vps.auth.mode={raw_mode!r} and no "
                f"vps.auth.groups; defaulting to DENY (admin-only). Valid "
                f"modes: public, private, admin-only."
            )
        else:
            warnings.append(
                f"[{app_name}]: no vps.auth.mode or vps.auth.groups label; "
                f"defaulting to DENY (admin-only). Add `vps.auth.groups=...` "
                f"(e.g. staff, client, accounting) or `vps.auth.mode=public`."
            )
        resolved_mode, allowed, is_public = "deny", ["admin"], False

    _LAST_WARNINGS.clear()
    _LAST_WARNINGS.extend(warnings)
    return resolved_mode, allowed, is_public


_LAST_WARNINGS: list[str] = []


def last_warnings() -> list[str]:
    """Warnings produced by the most recent `resolve_auth_mode` call.

    The list is overwritten on every call. Useful for callers that want to
    forward warnings to a logger / stderr without coupling resolve_auth_mode
    to a specific I/O sink."""
    return list(_LAST_WARNINGS)


# ─── Image-tag classification (managed-update eligibility) ────────────────

_FULL_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[.-][\w.-]+)?$")
_PARTIAL_SEMVER_RE = re.compile(r"^v?\d+(?:\.\d+)?(?:[.-][\w.-]+)?$")
_FLOATING_TAGS = frozenset({"latest", "stable", "alpine", "edge", "main", "master"})
POLICY_VALUES = frozenset({"patch", "minor", "major", "patch+minor", "off"})


def classify_image_tag(image: str) -> tuple[str, str]:
    """Return (tag_class, tag). tag_class is one of:
      - 'full_semver' : eligible for managed updates (X.Y.Z form)
      - 'partial'     : not eligible (X or X.Y only)
      - 'floating'    : not eligible (latest, stable, branch names, etc.)
      - 'unset'       : no tag specified (implicit :latest)

    Mirrors the engine's classification rules so what compose-lint says and
    what stack_update_managed actually does never drift apart.
    """
    if not image or not isinstance(image, str):
        return "unset", ""
    bare = image.rsplit("@", 1)[0]
    if ":" not in bare:
        return "unset", ""
    tag = bare.rsplit(":", 1)[1].strip()
    if not tag:
        return "unset", ""
    low = tag.lower()
    if low in _FLOATING_TAGS:
        return "floating", tag
    if _FULL_SEMVER_RE.match(tag):
        return "full_semver", tag
    if _PARTIAL_SEMVER_RE.match(tag):
        return "partial", tag
    return "floating", tag
