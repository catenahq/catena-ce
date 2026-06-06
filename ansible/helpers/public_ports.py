"""Declarative public-port registry: single source of truth for every
direct public port the VPS exposes outside the Cloudflare Tunnel.

Cloudflare Tunnel carries HTTP/S only. Protocols it cannot carry (SMTP,
IMAPS, TURN media, ...) bind directly to the VPS public IP. Before this
module each such port was hand-wired in four places (ufw, a DOCKER-USER
guard, the validate allowlist, the external nmap expectation) plus prose
docs -- a drift surface. This module collapses that to ONE declaration per
port, consumed everywhere.

Two feeders, one merged effective set:

  - Infra roles declare a `public_ports` list var (coturn, the Dokploy UI).
    Each entry is host-bound (the service uses host networking or a swarm
    host-mode publish), so ufw INPUT actually sees the traffic.
  - Dokploy templates declare `vps.expose.tcp/udp` compose labels. Those
    apps publish ports via Docker, whose DNAT bypasses the ufw INPUT chain,
    so enforcement (when the scope is restricted) happens in DOCKER-USER.
    App ports default to scope `any` (that is the only reason to expose
    them publicly).

The host reconciler (vps-scripts/catena-public-ports.py) reads the infra
JSON rendered by roles/common/tasks/public_ports.yml PLUS the live
`vps.expose.*` labels off running containers, merges them here, applies the
rule plan idempotently, and re-renders the effective-set JSON + the operator
inventory doc. validate.yml and tests/external/public-ports.yml read the
effective set off the host rather than a hand-list, so they can never drift
from what is actually open.

Stdlib-only by design so it installs on the host next to dashboard-sync.py
without dragging deps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

try:
    # Flat install on the host (next to dashboard-sync.py).
    from labels_schema import extract_vps_expose_labels, slugify
except ImportError:
    # In-repo / CI: helpers/ is a package (pyproject pythonpath = automation).
    from helpers.labels_schema import extract_vps_expose_labels, slugify

# RFC 1918 source blocks: Docker bridge networks (catena-admin, oauth2-proxy,
# the Dokploy UI dispatcher path) live here. Cannot be spoofed from the
# public internet, so a RETURN for these is safe.
_RFC1918 = ("172.16.0.0/12", "10.0.0.0/8")

VALID_PROTOS = ("tcp", "udp")
VALID_SCOPES = ("any", "tailnet", "rfc1918")
VALID_BINDS = ("host", "docker")


@dataclass(frozen=True, order=True)
class PortEntry:
    """One declared public port (or contiguous range).

    `lo == hi` for a single port. Ordered/hashable so a list of entries
    dedups + sorts deterministically (proto, lo, hi, scope, bind first --
    owner/comment are descriptive, not identity)."""

    proto: str
    lo: int
    hi: int
    scope: str = "any"
    bind: str = "host"
    owner: str = field(default="", compare=False)
    comment: str = field(default="", compare=False)

    @property
    def port_spec(self) -> str:
        """`lo:hi` for a range, else the single port -- the form ufw and
        iptables `--dport` both accept."""
        return f"{self.lo}:{self.hi}" if self.hi != self.lo else str(self.lo)

    def ports(self) -> list[int]:
        """Every individual port in the range (for the nmap expectation,
        whose scanner reports individual open ports)."""
        return list(range(self.lo, self.hi + 1))


class PortDeclError(ValueError):
    """A declaration was malformed. Raised by normalize_* so a typo fails
    loudly at converge time rather than silently opening the wrong port."""


def _norm_port_field(raw) -> tuple[int, int]:
    """Coerce a `port` field (int, "n", or "lo-hi"/"lo:hi") to (lo, hi)."""
    if isinstance(raw, int):
        lo = hi = raw
    else:
        s = str(raw).strip()
        sep = "-" if "-" in s else (":" if ":" in s else None)
        if sep:
            a, _, b = s.partition(sep)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(s)
    if not (1 <= lo <= 65535 and 1 <= hi <= 65535) or hi < lo:
        raise PortDeclError(f"port out of range or inverted: {raw!r}")
    return lo, hi


def normalize_infra(public_ports: list[dict] | None) -> list[PortEntry]:
    """Normalize the infra `public_ports` role var into PortEntry objects.

    Each raw entry: {proto, port, scope?, bind?, owner?, comment?}.
      - proto: tcp|udp (required)
      - port:  int | "n" | "lo-hi" | "lo:hi" (required)
      - scope: any|tailnet|rfc1918 (default any)
      - bind:  host|docker (default host -- infra ports are host-bound)
    Raises PortDeclError on any invalid field."""
    out: list[PortEntry] = []
    for raw in public_ports or []:
        proto = str(raw.get("proto", "")).lower()
        if proto not in VALID_PROTOS:
            raise PortDeclError(f"proto must be one of {VALID_PROTOS}: {raw!r}")
        if "port" not in raw:
            raise PortDeclError(f"missing `port`: {raw!r}")
        lo, hi = _norm_port_field(raw["port"])
        scope = str(raw.get("scope", "any")).lower()
        if scope not in VALID_SCOPES:
            raise PortDeclError(f"scope must be one of {VALID_SCOPES}: {raw!r}")
        bind = str(raw.get("bind", "host")).lower()
        if bind not in VALID_BINDS:
            raise PortDeclError(f"bind must be one of {VALID_BINDS}: {raw!r}")
        out.append(
            PortEntry(
                proto=proto,
                lo=lo,
                hi=hi,
                scope=scope,
                bind=bind,
                owner=str(raw.get("owner", "")),
                comment=str(raw.get("comment", "")),
            )
        )
    return out


def entries_from_labels(app_name: str, compose_text: str) -> list[PortEntry]:
    """Harvest vps.expose.tcp/udp labels from one app's compose into
    PortEntry objects. App-published ports are always scope=any, bind=docker
    (Docker publishes them; DNAT bypasses ufw INPUT)."""
    labels = extract_vps_expose_labels(compose_text)
    owner = slugify(app_name) if app_name else ""
    out: list[PortEntry] = []
    for proto in VALID_PROTOS:
        for lo, hi in labels.get(proto, []):
            out.append(
                PortEntry(
                    proto=proto,
                    lo=lo,
                    hi=hi,
                    scope="any",
                    bind="docker",
                    owner=owner,
                    comment="declared via vps.expose label",
                )
            )
    return out


def merge(*entry_lists: list[PortEntry]) -> list[PortEntry]:
    """Dedup (by identity = proto/lo/hi/scope/bind) and sort. When the same
    port is declared twice, the first occurrence's owner/comment wins (infra
    feeders should be listed before label feeders by the caller)."""
    seen: dict[tuple, PortEntry] = {}
    for lst in entry_lists:
        for e in lst:
            key = (e.proto, e.lo, e.hi, e.scope, e.bind)
            seen.setdefault(key, e)
    return sorted(seen.values())


# ─── Artifact rendering ────────────────────────────────────────────────────


def expected_open_ports(entries: list[PortEntry]) -> list[int]:
    """Sorted unique list of individual ports reachable from the PUBLIC
    internet -- i.e. scope == "any" only. This is the nmap expectation
    (`expected_public_ports`) AND the on-host host-port allowlist
    (`_r4_public_ports`): tailnet/rfc1918-scoped ports are NOT reachable
    from the public IP, so an external scan must NOT see them."""
    ports: set[int] = set()
    for e in entries:
        if e.scope == "any":
            ports.update(e.ports())
    return sorted(ports)


def bound_tcp_ports(entries: list[PortEntry]) -> list[int]:
    """Every individual TCP port BOUND on the host, any scope. This is what
    an on-host `ss -tlnp4` sees as a 0.0.0.0 listener, so it is the allowlist
    for validate.yml's host-port-binding drift check (which includes
    tailnet-scoped ports like the Dokploy UI -- they are bound, just
    firewall-restricted)."""
    ports: set[int] = set()
    for e in entries:
        if e.proto == "tcp":
            ports.update(e.ports())
    return sorted(ports)


def public_open_tcp_ports(entries: list[PortEntry]) -> list[int]:
    """Public (scope=any) TCP ports only -- the external nmap expectation.
    The external scanner is TCP-only, so UDP public ports (coturn) are not
    enumerated there."""
    ports: set[int] = set()
    for e in entries:
        if e.scope == "any" and e.proto == "tcp":
            ports.update(e.ports())
    return sorted(ports)


def summary_json(entries: list[PortEntry]) -> str:
    """Precomputed integer port lists for validation consumers, so the
    Ansible side never has to expand ranges in Jinja:
      - all_tcp_bound:  TCP ports bound on the host (any scope) -> the
        host-port-binding allowlist (plus 22/80/443, infra ports not in
        the registry).
      - public_open:    ports reachable from the public internet (scope=any,
        tcp+udp) -> documentation / completeness.
      - public_open_tcp: scope=any TCP ports -> the external nmap expectation
        (the scanner is TCP-only)."""
    return json.dumps(
        {"all_tcp_bound": bound_tcp_ports(entries),
         "public_open": expected_open_ports(entries),
         "public_open_tcp": public_open_tcp_ports(entries)},
        indent=2,
    )


def to_effective_json(entries: list[PortEntry]) -> str:
    """Serialize the merged set for the host (effective-set JSON the
    reconciler writes and validate.yml slurps)."""
    return json.dumps(
        [
            {
                "proto": e.proto,
                "port": e.port_spec,
                "scope": e.scope,
                "bind": e.bind,
                "owner": e.owner,
                "comment": e.comment,
            }
            for e in entries
        ],
        indent=2,
        sort_keys=False,
    )


def from_effective_json(text: str) -> list[PortEntry]:
    """Inverse of to_effective_json (used by the reconciler to load the
    infra config file)."""
    return normalize_infra(json.loads(text))


def render_doc(entries: list[PortEntry]) -> str:
    """Generated operator inventory: the "know what's what" table. Never
    hand-maintained -- regenerated from the effective set on every reconcile
    / converge."""
    lines = [
        "# Public ports (generated -- do not edit)",
        "",
        "Every port the VPS exposes outside the Cloudflare Tunnel, with who",
        "owns it and from where it is reachable. Generated from the public-port",
        "registry (infra `public_ports` vars + `vps.expose.*` template labels)",
        "by helpers/public_ports.py. Edit the declarations, not this file.",
        "",
        "- **scope `any`** -- reachable from the public internet.",
        "- **scope `tailnet`** -- tailscale0 + RFC1918 only (enforced; public refused).",
        "- **scope `rfc1918`** -- Docker bridge networks only.",
        "",
        "| Proto | Port | Scope | Bind | Owner | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        lines.append(
            f"| {e.proto} | {e.port_spec} | {e.scope} | {e.bind} "
            f"| {e.owner or '-'} | {e.comment or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ─── Rule plan (data; applied by the host reconciler) ──────────────────────
#
# A rule plan is a list of dicts the reconciler turns into ufw / iptables
# invocations. Kept as DATA (not side effects) so the mapping is unit-tested
# without root.
#
# A published port can be reached two ways, so two layers exist:
#   - INPUT chain (ufw): the host-bound listener, OR a docker-published
#     port arriving via the userspace docker-proxy. ufw filters this path
#     for BOTH bind types, by scope.
#   - FORWARD / DOCKER-USER: docker DNATs published-port packets in
#     PREROUTING, bypassing INPUT entirely. ufw never sees them, so a
#     RESTRICTED docker port needs an ADDITIONAL DOCKER-USER guard.
#
# Enforcement matrix (ufw layer by scope, applied to host AND docker):
#   any      -> ufw allow <spec>/<proto> (public)
#   tailnet  -> ufw allow in on tailscale0 + ufw allow from rfc1918
#   rfc1918  -> ufw allow from rfc1918
# Plus, for docker bind only, the DNAT-path guard:
#   any      -> nothing (Docker default-publishes the DNAT path open)
#   tailnet  -> DOCKER-USER: RETURN tailscale0, RETURN rfc1918, DROP
#   rfc1918  -> DOCKER-USER: RETURN rfc1918, DROP
#
# This mirrors the proven two-layer port-3000 guard (ufw tailscale0 +
# DOCKER-USER RETURN/DROP). DROP-last ordering matches it.


def _ufw_layer(e: PortEntry) -> list[dict]:
    """ufw INPUT-chain rules for one entry, by scope (bind-independent)."""
    if e.scope == "any":
        return [{"engine": "ufw", "action": "allow", "proto": e.proto,
                 "port": e.port_spec, "from": "any", "owner": e.owner}]
    rules: list[dict] = []
    if e.scope == "tailnet":
        rules.append({"engine": "ufw", "action": "allow", "proto": e.proto,
                      "port": e.port_spec, "iface": "tailscale0", "owner": e.owner})
    for net in _RFC1918:
        rules.append({"engine": "ufw", "action": "allow", "proto": e.proto,
                      "port": e.port_spec, "from": net, "owner": e.owner})
    return rules


def _docker_user_layer(e: PortEntry) -> list[dict]:
    """DOCKER-USER (DNAT path) guard for a RESTRICTED docker-bound entry.
    RETURN allowed sources first, DROP last. Empty for scope=any (DNAT path
    is open by Docker default) or host bind (no DNAT)."""
    if e.bind != "docker" or e.scope == "any":
        return []
    rules: list[dict] = []
    if e.scope == "tailnet":
        rules.append({"engine": "docker-user", "action": "RETURN", "proto": e.proto,
                      "port": e.port_spec, "iface": "tailscale0", "owner": e.owner})
    for net in _RFC1918:
        rules.append({"engine": "docker-user", "action": "RETURN", "proto": e.proto,
                      "port": e.port_spec, "from": net, "owner": e.owner})
    rules.append({"engine": "docker-user", "action": "DROP", "proto": e.proto,
                  "port": e.port_spec, "owner": e.owner})
    return rules


def rule_plan(entries: list[PortEntry]) -> list[dict]:
    """Ordered firewall rules for the merged set: the ufw layer for every
    entry, then each restricted docker entry's DOCKER-USER guard."""
    plan: list[dict] = []
    for e in entries:
        plan.extend(_ufw_layer(e))
        plan.extend(_docker_user_layer(e))
    return plan
