#!/usr/bin/env python3
"""Host reconciler for the declarative public-port registry.

Single applier for every direct public port. Reads two feeders, merges them
via helpers/public_ports.py, and idempotently applies the firewall rule plan:

  1. Infra declarations -- JSON fragments under /etc/catena/public-ports.d/
     (one per owner, e.g. coturn.json, dokploy.json), dropped by each infra
     role at converge. conf.d style so roles declare independently with no
     ordering coupling.
  2. Live app ports -- vps.expose.tcp/udp labels harvested off running
     containers, so deploying a template opens its ports without a converge.

Then it:
  - applies ufw allow rules for host-bound ports (ufw INPUT sees them),
  - applies DOCKER-USER RETURN/DROP guards for docker-bound RESTRICTED ports
    (Docker DNAT bypasses ufw INPUT). docker-bound scope=any ports need no
    rule -- Docker publishes them open, and they close automatically when the
    container stops, which is exactly "deploy = open, remove = close".
  - writes the effective set to /etc/catena/public-ports.effective.json and a
    human inventory to /etc/catena/public-ports.md, both read by validation.

Fired on every docker.service start (Docker recreates DOCKER-USER empty on
boot) via a drop-in, plus a periodic timer. Generalizes the older
dokploy-docker-firewall.sh (port-3000-only) guard. Runs as root.

Stdlib-only. Imports public_ports + labels_schema, installed flat alongside
this script (sys.path is pinned to the script dir below).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import public_ports as pp  # noqa: E402

FRAGMENT_DIR = os.environ.get("CATENA_PORTS_FRAGMENT_DIR", "/etc/catena/public-ports.d")
EFFECTIVE_JSON = os.environ.get(
    "CATENA_PORTS_EFFECTIVE_JSON", "/etc/catena/public-ports.effective.json"
)
EFFECTIVE_DOC = os.environ.get("CATENA_PORTS_EFFECTIVE_DOC", "/etc/catena/public-ports.md")
EFFECTIVE_SUMMARY = os.environ.get(
    "CATENA_PORTS_EFFECTIVE_SUMMARY", "/etc/catena/public-ports.summary.json"
)
# Last-applied rule plan. Diffing against it lets a reconcile DELETE rules
# whose declaration went away (e.g. a Minecraft template removed), so the
# firewall fully auto-heals instead of accumulating stale allows.
APPLIED_STATE = os.environ.get(
    "CATENA_PORTS_APPLIED_STATE", "/etc/catena/public-ports.applied.json"
)
UFW_COMMENT_PREFIX = "catena-ports"


def log(msg: str) -> None:
    print(f"catena-public-ports: {msg}", flush=True)


def _run(argv: list[str], check_only: bool = False) -> int:
    """Run argv, return rc. Never raises on non-zero (callers branch on rc)."""
    return subprocess.run(argv, capture_output=True, text=True).returncode


# ─── Feeder 1: infra fragments ─────────────────────────────────────────────


def load_infra_fragments(fragment_dir: str = FRAGMENT_DIR) -> list[pp.PortEntry]:
    entries: list[pp.PortEntry] = []
    for path in sorted(glob.glob(os.path.join(fragment_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                entries.extend(pp.normalize_infra(json.load(fh)))
        except (OSError, ValueError, pp.PortDeclError) as exc:
            log(f"WARNING: skipping malformed fragment {path}: {exc}")
    return entries


# ─── Feeder 2: live container labels ───────────────────────────────────────


def harvest_label_entries() -> list[pp.PortEntry]:
    """Read vps.expose.* labels off running containers via `docker inspect`.
    Each container's labels are flattened to `key=value` lines and fed to the
    same parser the templates use, so validation logic is shared."""
    ids = subprocess.run(
        ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
        capture_output=True, text=True,
    )
    if ids.returncode != 0:
        log("docker ps failed (daemon not up yet?); no label feeder this cycle")
        return []
    entries: list[pp.PortEntry] = []
    for line in ids.stdout.splitlines():
        if "\t" not in line:
            continue
        cid, name = line.split("\t", 1)
        insp = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", cid],
            capture_output=True, text=True,
        )
        if insp.returncode != 0:
            continue
        try:
            labels = json.loads(insp.stdout.strip() or "null") or {}
        except ValueError:
            continue
        if not isinstance(labels, dict):
            continue
        blob = "\n".join(f"{k}={v}" for k, v in labels.items())
        entries.extend(pp.entries_from_labels(name, blob))
    return entries


# ─── Rule signatures + applied-state (for auto-healing GC) ─────────────────


def rule_sig(rule: dict) -> tuple:
    """Identity of a firewall effect, ignoring the descriptive owner. Two
    rules with the same signature have the same firewall effect, so an
    owner-only change does not churn iptables/ufw."""
    return (rule["engine"], rule.get("action"), rule["proto"], rule["port"],
            rule.get("iface"), rule.get("from"))


def load_applied() -> list[dict]:
    try:
        with open(APPLIED_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_applied(plan: list[dict]) -> None:
    os.makedirs(os.path.dirname(APPLIED_STATE), exist_ok=True)
    tmp = f"{APPLIED_STATE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, APPLIED_STATE)


# ─── ufw application (host-bound ports) ────────────────────────────────────


def _ufw_spec(rule: dict) -> list[str]:
    """The `allow ...` spec (no leading `ufw`, no comment). add prepends
    `ufw`; delete prepends `ufw delete`."""
    proto, port = rule["proto"], rule["port"]
    if rule.get("iface"):
        return ["allow", "in", "on", rule["iface"], "proto", proto,
                "to", "any", "port", port]
    if rule.get("from", "any") != "any":
        return ["allow", "from", rule["from"], "proto", proto,
                "to", "any", "port", port]
    return ["allow", f"{port}/{proto}"]


def _ufw_argv(rule: dict) -> list[str]:
    """Full `ufw allow ... comment <tag>` argv for adding a rule."""
    comment = f"{UFW_COMMENT_PREFIX}:{rule.get('owner') or 'infra'}"
    return ["ufw", *_ufw_spec(rule), "comment", comment]


def apply_ufw(rules: list[dict]) -> None:
    """ufw is idempotent (skips existing rules), so a plain add converges."""
    for rule in rules:
        if _run(_ufw_argv(rule)) != 0:
            log(f"WARNING: ufw add failed: {rule}")


def prune_ufw(stale: list[dict]) -> None:
    """Delete ufw rules whose declaration went away. We know exactly what we
    added (from applied-state), so delete by the same spec -- no fragile
    `ufw status` parsing. ufw delete ignores the comment."""
    for rule in stale:
        if _run(["ufw", "delete", *_ufw_spec(rule)]) == 0:
            log(f"pruned ufw {rule['proto']}/{rule['port']}")


# ─── DOCKER-USER application (docker-bound restricted ports) ───────────────


def _docker_user_match(rule: dict) -> list[str]:
    """The match portion (everything except -A/-C/-D DOCKER-USER and -j)."""
    argv: list[str] = []
    if rule.get("iface"):
        argv += ["-i", rule["iface"]]
    if rule.get("from"):
        argv += ["-s", rule["from"]]
    argv += ["-p", rule["proto"], "--dport", rule["port"]]
    return argv


def _docker_user_chain_present() -> bool:
    return _run(["iptables", "-nL", "DOCKER-USER"]) == 0


def apply_docker_user(rules: list[dict]) -> None:
    """Append RETURN/DROP guards in plan order (RETURN before DROP), each
    guarded by `iptables -C` so re-runs are no-ops. Docker recreates the
    chain empty on restart; on a clean chain the append order is correct."""
    if not rules:
        return
    if not _docker_user_chain_present():
        log("DOCKER-USER chain absent (docker not up yet); deferring to next start")
        return
    for rule in rules:
        full = ["DOCKER-USER", *_docker_user_match(rule), "-j", rule["action"]]
        if _run(["iptables", "-C", *full]) != 0:
            if _run(["iptables", "-A", *full]) == 0:
                log(f"appended DOCKER-USER {rule['action']} {rule['proto']}/{rule['port']}")
            else:
                log(f"WARNING: iptables append failed: {full}")


def prune_docker_user(stale: list[dict]) -> None:
    """Delete DOCKER-USER guards whose declaration went away. -D is a no-op
    (nonzero, ignored) if the chain was flushed by a docker restart."""
    if not stale or not _docker_user_chain_present():
        return
    for rule in stale:
        full = ["DOCKER-USER", *_docker_user_match(rule), "-j", rule["action"]]
        if _run(["iptables", "-C", *full]) == 0 and _run(["iptables", "-D", *full]) == 0:
            log(f"pruned DOCKER-USER {rule['action']} {rule['proto']}/{rule['port']}")


# ─── Effective-set output (read by validation) ─────────────────────────────


def write_effective(entries: list[pp.PortEntry]) -> None:
    for path, content in (
        (EFFECTIVE_JSON, pp.to_effective_json(entries)),
        (EFFECTIVE_DOC, pp.render_doc(entries)),
        (EFFECTIVE_SUMMARY, pp.summary_json(entries)),
    ):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
        os.replace(tmp, path)


def reconcile() -> list[pp.PortEntry]:
    infra = load_infra_fragments()
    labels = harvest_label_entries()
    entries = pp.merge(infra, labels)  # infra wins ties
    plan = pp.rule_plan(entries)

    # Auto-heal: delete rules whose declaration disappeared since last run
    # (e.g. a removed template). Diff by signature so an owner-only change
    # doesn't churn. Prune BEFORE add so a port that flipped scope is
    # cleanly re-laid.
    new_sigs = {rule_sig(r) for r in plan}
    stale = [r for r in load_applied() if rule_sig(r) not in new_sigs]
    prune_ufw([r for r in stale if r["engine"] == "ufw"])
    prune_docker_user([r for r in stale if r["engine"] == "docker-user"])

    apply_ufw([r for r in plan if r["engine"] == "ufw"])
    apply_docker_user([r for r in plan if r["engine"] == "docker-user"])
    save_applied(plan)
    write_effective(entries)
    log(
        f"reconciled {len(entries)} port entries "
        f"({len([e for e in entries if e.scope == 'any'])} public)"
    )
    return entries


if __name__ == "__main__":
    reconcile()
