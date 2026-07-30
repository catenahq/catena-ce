#!/usr/bin/env python3
"""Host reconciler for the declarative public-port registry.

Single applier for every direct public port. Reads two feeders, merges them
via helpers/public_ports.py, and idempotently applies the firewall rule plan:

  1. Infra declarations -- JSON fragments under /etc/catena/public-ports.d/
     (one per owner, e.g. coturn.json, portainer.json), dropped by each infra
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
port-3000-only firewall guard it replaced. Runs as root.

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
    """The `<action> ...` spec (no leading `ufw`, no comment). add prepends
    `ufw`; delete prepends `ufw delete`.

    The action is READ from the rule, not hardcoded. A loopback-scoped port
    is enforced by a deny, and a spec that always said `allow` would not
    merely fail to guard it -- it would run `ufw allow` on the port it was
    asked to close, and then record that as applied.
    """
    action = rule.get("action", "allow")
    proto, port = rule["proto"], rule["port"]
    if rule.get("iface"):
        return [action, "in", "on", rule["iface"], "proto", proto,
                "to", "any", "port", port]
    if rule.get("from", "any") != "any":
        return [action, "from", rule["from"], "proto", proto,
                "to", "any", "port", port]
    return [action, f"{port}/{proto}"]


def _ufw_argv(rule: dict) -> list[str]:
    """Full `ufw allow ... comment <tag>` argv for adding a rule."""
    comment = f"{UFW_COMMENT_PREFIX}:{rule.get('owner') or 'infra'}"
    return ["ufw", *_ufw_spec(rule), "comment", comment]


def apply_ufw(rules: list[dict]) -> list[dict]:
    """ufw is idempotent (skips existing rules), so a plain add converges.

    Returns the rules actually applied. A failed add used to log WARNING and
    be recorded as applied anyway, so the applied-state, the effective
    artifacts validation reads, and the unit's exit code all agreed that a
    rule existed which did not.
    """
    applied: list[dict] = []
    for rule in rules:
        if _run(_ufw_argv(rule)) != 0:
            log(f"WARNING: ufw add failed: {rule}")
            continue
        applied.append(rule)
    return applied


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


def apply_docker_user(rules: list[dict]) -> list[dict]:
    """Append RETURN/DROP guards in plan order (RETURN before DROP), each
    guarded by `iptables -C` so re-runs are no-ops. Docker recreates the
    chain empty on restart; on a clean chain the append order is correct.

    Returns the rules actually applied. These are the guards that keep a
    docker-bound RESTRICTED port off the public internet -- Docker's DNAT
    bypasses ufw INPUT entirely, so with no DOCKER-USER rule the port is
    simply open. Returning early on an absent chain and letting the caller
    record the plan as applied meant a host could publish every restricted
    port while its own artifacts said otherwise.
    """
    if not rules:
        return []
    if not _docker_user_chain_present():
        log("ERROR: DOCKER-USER chain absent; "
            f"{len(rules)} restricted port guard(s) NOT applied and every "
            "port they cover is reachable. Deferred to the next docker.service "
            "start, which the drop-in fires -- but until then this is open.")
        return []
    applied: list[dict] = []
    for rule in rules:
        full = ["DOCKER-USER", *_docker_user_match(rule), "-j", rule["action"]]
        if _run(["iptables", "-C", *full]) == 0:
            applied.append(rule)
            continue
        if _run(["iptables", "-A", *full]) == 0:
            log(f"appended DOCKER-USER {rule['action']} {rule['proto']}/{rule['port']}")
            applied.append(rule)
        else:
            log(f"WARNING: iptables append failed: {full}")
    return applied


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


def write_effective(entries: list[pp.PortEntry], unapplied: int = 0) -> None:
    """Write the three artifacts validation reads.

    They describe what the registry DECLARES. `unapplied` rides in the
    summary because it is the only field that says whether the host is
    actually in that state -- without it a scan of a host whose DOCKER-USER
    guards never installed reads exactly like a scan of a correct one.
    """
    for path, content in (
        (EFFECTIVE_JSON, pp.to_effective_json(entries)),
        (EFFECTIVE_DOC, pp.render_doc(entries)),
        (EFFECTIVE_SUMMARY, pp.summary_json(entries, rules_unapplied=unapplied)),
    ):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
        os.replace(tmp, path)


def reconcile() -> tuple[list[pp.PortEntry], int]:
    """Apply the plan. Returns (effective entries, count NOT applied)."""
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

    applied = apply_ufw([r for r in plan if r["engine"] == "ufw"])
    applied += apply_docker_user([r for r in plan if r["engine"] == "docker-user"])

    # Record what was APPLIED, not what was planned. The two used to be the
    # same variable, so a rule that failed to install was indistinguishable
    # from one that installed cleanly on every subsequent run.
    save_applied(applied)
    unapplied = len(plan) - len(applied)
    write_effective(entries, unapplied)

    log(
        f"reconciled {len(entries)} port entries "
        f"({len([e for e in entries if e.scope == 'any'])} public), "
        f"{len(applied)}/{len(plan)} firewall rules applied"
    )
    if unapplied:
        log(f"ERROR: {unapplied} firewall rule(s) NOT applied -- the effective "
            f"set written to {EFFECTIVE_JSON} describes what is DECLARED, and "
            "the host is not in that state")
    return entries, unapplied


if __name__ == "__main__":
    # Non-zero when the plan was not fully applied. The unit exiting 0 while
    # restricted ports sat unguarded is the whole defect: systemd recorded
    # success, validation read artifacts that described the declared state,
    # and nothing anywhere said the rules were missing.
    _entries, _unapplied = reconcile()
    sys.exit(1 if _unapplied else 0)
