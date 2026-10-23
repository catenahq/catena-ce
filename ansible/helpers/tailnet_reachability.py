#!/usr/bin/env python3
"""Ask the tailnet whether this host is reachable on it, before public 22 closes.

Runs ON the host, shipped by bootstrap/roles/tailscale/tasks/reachable.yml,
which playbooks/lockdown.yml runs before it closes public 22. Each question is
answered by something other than the host's own view of itself:

  1. The control server's record of this node. Tailscale: the device API's
     `connectedToControl` and `blocksIncomingConnections`. Headscale: the node
     API's `online`. A node the control server does not see connected, or one
     that refuses incoming connections, is reachable by nobody.
  2. With TAILNET_REQUIRE_PEER=1 (what reachable.yml sets): an online peer that
     carries none of this host's tags answers a TSMP ping through the tunnel.
  3. With TAILNET_REQUIRE_PEER=1: the tailnet policy lets one of those peers
     open TCP 22 to one of this host's tailnet addresses. tailscaled answers
     the ping in 2 before its packet filter runs, and the control server
     reports the node online whatever its policy, so neither sees a policy
     that keeps SSH out. policy_reasons reads the filter rules the control
     server sent this host (`tailscale debug netmap`, PacketFilterRules) and
     matches them as tailscale's wgengine/filter does, a node-capability
     source matching nobody; a filter it cannot read or parse is a refusal.
  4. With TAILNET_REQUIRE_PEER=1: this host's own firewall lets SSH in over
     tailscale0. The ping in 2 is answered inside tailscaled and never crosses
     that interface, so it cannot see a rule that drops the traffic there.
     firewall_reasons walks a new TCP connection to port 22, arriving on
     tailscale0 and addressed to this host's tailnet address, through the
     iptables and ip6tables rulesets in the order the kernel does.

Input is one JSON object on stdin, so no credential reaches an argv, an
environment or the process table: TAILNET_PROVIDER (tailscale | headscale),
TAILSCALE_API_BASE, TAILSCALE_OAUTH_CLIENT_ID, TAILSCALE_OAUTH_CLIENT_SECRET,
HEADSCALE_URL, HEADSCALE_API_KEY, TAILNET_OWN_TAGS (comma-separated),
TAILNET_REQUIRE_PEER.

Prints one JSON object on stdout, {"ok", "reasons", "summary"}, and exits 0
whatever the verdict; the caller asserts on it. Nothing it prints carries a
credential.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

# A node that has just run `tailscale up` reaches the control server, and its
# netmap its peers, a few seconds later.
ATTEMPTS = 7
DELAY_S = 10.0
# At most this many peers are pinged per attempt.
MAX_PEERS = 5

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
Http = Callable[..., tuple[int, object]]


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          check=False)


def _http(method: str, url: str, *, headers: dict | None = None,
          data: bytes | None = None) -> tuple[int, object]:
    """(status, parsed JSON or None). Status 0 means no answer arrived."""
    req = urllib.request.Request(url, method=method, data=data,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    try:
        return status, json.loads(body or b"null")
    except ValueError:
        return status, None


def local_status(run: Run) -> dict:
    r = run(["tailscale", "status", "--json"])
    if r.returncode != 0:
        raise RuntimeError(
            f"`tailscale status --json` exited {r.returncode}: "
            f"{(r.stderr or '').strip()[-300:]}")
    return json.loads(r.stdout)


def tailscale_control(status: dict, cfg: dict, http: Http) -> list[str]:
    """Reasons the Tailscale control server gives for this node being
    unreachable, or [] when it sees the node connected and accepting."""
    node_id = str((status.get("Self") or {}).get("ID") or "")
    if not node_id:
        return ["`tailscale status` names no node id for this host"]
    base = (cfg.get("TAILSCALE_API_BASE") or "https://api.tailscale.com/api/v2").rstrip("/")
    client_id = cfg.get("TAILSCALE_OAUTH_CLIENT_ID") or ""
    secret = cfg.get("TAILSCALE_OAUTH_CLIENT_SECRET") or ""
    if not (client_id and secret):
        return ["no Tailscale OAuth client is stored, so the control server "
                "cannot be asked"]
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    code, token = http(
        "POST", f"{base}/oauth/token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
    if code != 200 or not isinstance(token, dict) or not token.get("access_token"):
        return [f"the OAuth client did not exchange for a token (HTTP {code})"]
    code, device = http(
        "GET", f"{base}/device/{urllib.parse.quote(node_id)}?fields=all",
        headers={"Authorization": f"Bearer {token['access_token']}"})
    if code == 403:
        return ["the OAuth client cannot read devices: add Devices > Core "
                "(read) to its scopes in the Tailscale admin console"]
    if code != 200 or not isinstance(device, dict):
        return [f"the control server has no readable record of device "
                f"{node_id} (HTTP {code})"]
    reasons = []
    if not device.get("connectedToControl"):
        reasons.append("the control server does not see this server connected")
    if device.get("blocksIncomingConnections"):
        reasons.append("this server refuses incoming tailnet connections "
                       "(shields up), so no device can reach it")
    return reasons


def headscale_control(status: dict, cfg: dict, http: Http) -> list[str] | None:
    """Reasons the Headscale server gives, [] when it sees the node online,
    or None when no API key is stored to ask with."""
    key = cfg.get("HEADSCALE_API_KEY") or ""
    if not key:
        return None
    node_key = str((status.get("Self") or {}).get("PublicKey") or "")
    base = (cfg.get("HEADSCALE_URL") or "").rstrip("/")
    code, body = http("GET", f"{base}/api/v1/node",
                      headers={"Authorization": f"Bearer {key}"})
    if code != 200 or not isinstance(body, dict):
        return [f"the Headscale server did not list its nodes (HTTP {code})"]
    node = next((n for n in body.get("nodes") or []
                 if n.get("nodeKey") == node_key), None)
    if node is None:
        return ["the Headscale server has no node with this server's key"]
    if not node.get("online"):
        return ["the Headscale server does not see this server online"]
    return []


def eligible_peers(status: dict, own_tags: set[str]) -> list[tuple[str, list[str]]]:
    """(name, tailnet addresses, IPv4 first) of every online peer that has an
    IPv4 address and carries none of this host's tags: a device an
    administrator could be on, not another server like this one."""
    out = []
    for peer in (status.get("Peer") or {}).values():
        if not peer.get("Online"):
            continue
        if own_tags & set(peer.get("Tags") or []):
            continue
        addrs = sorted(peer.get("TailscaleIPs") or [], key=lambda a: ":" in a)
        if addrs and "." in addrs[0]:
            out.append((peer.get("HostName") or addrs[0], addrs))
    return out


def ping_a_peer(peers: list[tuple[str, list[str]]], run: Run) -> str:
    """The name of the first peer that answers a TSMP ping, or ""."""
    for name, addrs in peers[:MAX_PEERS]:
        r = run(["tailscale", "ping", "--tsmp", "--c", "3", "--timeout", "5s", addrs[0]])
        if r.returncode == 0:
            return name
    return ""


TAILNET_IFACE = "tailscale0"
SSH_PORT = 22
# Where a packet addressed to this host goes, table by table, in kernel order.
_INBOUND = (("raw", "PREROUTING"), ("mangle", "PREROUTING"), ("nat", "PREROUTING"),
            ("mangle", "INPUT"), ("filter", "INPUT"))
# Targets that act on the packet and let it carry on through the chain.
_CONTINUE = {"LOG", "NFLOG", "MARK", "CONNMARK", "CT", "TRACE", "AUDIT", "DNAT",
             "SNAT", "MASQUERADE", "REDIRECT"}


def parse_ruleset(text: str) -> dict[str, dict]:
    """`iptables-save` output as {table: {"policy": {chain: policy},
    "rules": {chain: [rule line]}}}."""
    tables: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("*"):
            cur = tables.setdefault(line[1:], {"policy": {}, "rules": {}})
        elif cur is None or not line or line.startswith("#"):
            continue
        elif line.startswith(":"):
            chain, policy = line[1:].split()[:2]
            cur["policy"][chain] = policy
            cur["rules"].setdefault(chain, [])
        elif line.startswith("-A "):
            cur["rules"].setdefault(line.split()[1], []).append(line)
    return tables


def _ports(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in spec.split(","):
        lo, _, hi = part.partition(":")
        out.append((int(lo or 0), int(hi or lo or 65535)))
    return out


def rule_matches(tokens: list[str], own: list) -> bool | None:
    """Whether the rule matches the SSH packet: True, False, or None when a
    condition it carries cannot be decided here (a source address, a rate
    limit, a mark...)."""
    result: bool | None = True
    negate = False

    def fold(value: bool | None) -> None:
        nonlocal result, negate
        if value is not None and negate:
            value = not value
        negate = False
        if value is False:
            result = False
        elif value is None and result is True:
            result = None

    i = 0
    while i < len(tokens):
        opt = tokens[i]
        arg = tokens[i + 1] if i + 1 < len(tokens) else ""
        if opt in ("-j", "-g"):
            break  # the target and its own options come last
        if opt == "!":
            negate = True
            i += 1
            continue
        if opt in ("-A", "-m", "--comment"):
            i += 2
            continue
        if opt in ("-i", "--in-interface"):
            fold(TAILNET_IFACE.startswith(arg[:-1]) if arg.endswith("+")
                 else arg == TAILNET_IFACE)
        elif opt in ("-p", "--protocol"):
            fold(arg in ("tcp", "6", "all", "0"))
        elif opt in ("-d", "--destination"):
            nets = [ipaddress.ip_network(n, strict=False) for n in arg.split(",")]
            fold(any(a in n for a in own for n in nets if a.version == n.version))
        elif opt in ("--dport", "--destination-port", "--dports", "--destination-ports"):
            fold(any(lo <= SSH_PORT <= hi for lo, hi in _ports(arg)))
        elif opt in ("--ctstate", "--state"):
            fold("NEW" in arg.split(","))
        elif opt == "--dst-type":
            fold(True if arg == "LOCAL" else None)
        else:
            # An option this walk does not model, with every argument it took.
            fold(None)
            i += 1
            while i < len(tokens) and not tokens[i].startswith("-") and tokens[i] != "!":
                i += 1
            continue
        i += 2
    return result


def walk_inbound(tables: dict, own: list) -> list[str]:
    """Why the SSH packet would not reach sshd; [] when the ruleset accepts it.

    A condition the walk cannot decide never counts as accepting the packet:
    an undecidable ACCEPT is passed over, and an undecidable DROP or REJECT is
    reported."""
    reasons: list[str] = []

    def chain(table: str, name: str, depth: int) -> str:
        """'ACCEPT', 'DROP', or 'RETURN' when the chain ends without a verdict."""
        for line in tables[table]["rules"].get(name, []):
            tokens = shlex.split(line)
            flag = "-g" if "-g" in tokens else "-j"
            if flag not in tokens:
                continue
            target = tokens[tokens.index(flag) + 1]
            m = rule_matches(tokens, own)
            if m is False:
                continue
            if target in ("DROP", "REJECT"):
                reasons.append(f"{table}: {line}")
                if m:
                    return "DROP"
            elif target in ("ACCEPT", "RETURN"):
                if m:
                    return target
            elif target not in _CONTINUE and target in tables[table]["rules"]:
                if depth > 30:
                    reasons.append(f"{table}: {name} jumps deeper than 30 chains")
                    return "DROP"
                verdict = chain(table, target, depth + 1)
                if m and (verdict in ("ACCEPT", "DROP") or flag == "-g"):
                    return verdict
        return "RETURN"

    for table, name in _INBOUND:
        if name not in tables.get(table, {}).get("rules", {}):
            continue
        verdict = chain(table, name, 0)
        if verdict == "DROP":
            break
        if verdict == "RETURN" and tables[table]["policy"].get(name) == "DROP":
            reasons.append(f"{table}: nothing in {name} accepts it and its "
                           f"policy is DROP")
            break
        if verdict == "ACCEPT" and table == "filter":
            break
    return list(dict.fromkeys(reasons))


def firewall_reasons(status: dict, run: Run) -> list[str]:
    """Reasons this host's firewall keeps SSH over the tailnet out, [] when
    both rulesets let it in."""
    own = [ipaddress.ip_address(a)
           for a in (status.get("Self") or {}).get("TailscaleIPs") or []]
    if not own:
        return ["`tailscale status` names no tailnet address for this server, so "
                "its firewall cannot be checked"]
    reasons = []
    for tool, version in (("iptables-save", 4), ("ip6tables-save", 6)):
        addrs = [a for a in own if a.version == version]
        if not addrs:
            continue
        try:
            r = run([tool])
        except OSError as e:
            reasons.append(f"could not read this server's firewall ({tool}: {e})")
            continue
        if r.returncode != 0:
            reasons.append(f"could not read this server's firewall ({tool} "
                           f"exited {r.returncode})")
            continue
        for why in walk_inbound(parse_ruleset(r.stdout), addrs):
            reasons.append(f"this server's firewall drops SSH arriving over "
                           f"the tailnet ({tool}, {why})")
    return reasons


TCP = 6
# tailcfg.FilterRule.IPProto numbers, named in a refusal.
_PROTO_NAMES = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6", 132: "sctp"}


def _holds(spec: str, addr) -> bool:
    """Whether a filter rule's address form holds `addr`: "*", a CIDR, a range
    "first-last" or one address (tailcfg.FilterRule.SrcIPs). A "cap:" source
    names a node capability, which this check does not resolve, so it holds
    nothing; neither does a form tailscaled rejects."""
    if spec == "*":
        return True
    if spec.startswith("cap:"):
        return False
    try:
        if "/" in spec:
            return addr in ipaddress.ip_network(spec)
        if spec.count("-") == 1:
            lo, hi = (ipaddress.ip_address(a) for a in spec.split("-"))
            return lo.version == hi.version == addr.version and lo <= addr <= hi
        return ipaddress.ip_address(spec) == addr
    except ValueError:
        return False


def _allows_ssh(rule: dict, src, dst) -> bool:
    """Whether one tailcfg.FilterRule lets `src` open TCP 22 to `dst`. An
    empty IPProto is TCP, UDP and ICMP. A capability grant carries no
    DstPorts, so it lets no connection in."""
    protos = rule.get("IPProto") or []
    if protos and TCP not in protos:
        return False
    if not any(_holds(s, src) for s in rule.get("SrcIPs") or []):
        return False
    return any(_holds(d["IP"], dst)
               and d["Ports"]["First"] <= SSH_PORT <= d["Ports"]["Last"]
               for d in rule.get("DstPorts") or [])


def _brief(items: list[str], most: int = 3) -> str:
    more = len(items) - most
    return ", ".join(items[:most]) + (f" and {more} more" if more > 0 else "")


def _port_range(pr: dict) -> str:
    first, last = pr["First"], pr["Last"]
    if (first, last) == (0, 65535):
        return "*"
    return str(first) if first == last else f"{first}-{last}"


def _describe(rule: dict) -> str:
    srcs = _brief(rule.get("SrcIPs") or []) or "no source"
    if not rule.get("DstPorts"):
        return f"[capability grants only, from {srcs}]"
    protos = "/".join(_PROTO_NAMES.get(p, str(p))
                      for p in rule.get("IPProto") or []) or "tcp/udp/icmp"
    dsts = _brief([f"{d['IP']}:{_port_range(d['Ports'])}" for d in rule["DstPorts"]])
    return f"[{protos} from {srcs} to {dsts}]"


def policy_reasons(status: dict, peers: list[tuple[str, list[str]]],
                   own_tags: set[str], run: Run) -> list[str]:
    """Reasons the packet filter this host received from its control server
    keeps every peer in `peers` from opening TCP 22 to it, [] when one of them
    can. A connection stays in one address family, so each peer address is
    paired with this host's addresses of the same family."""
    own = [ipaddress.ip_address(a)
           for a in (status.get("Self") or {}).get("TailscaleIPs") or []]
    pairs = [(src, dst) for _, addrs in peers
             for src in map(ipaddress.ip_address, addrs)
             for dst in own if src.version == dst.version]
    r = run(["tailscale", "debug", "netmap"])
    if r.returncode != 0:
        return [f"could not read the tailnet policy this server received "
                f"(`tailscale debug netmap` exited {r.returncode}: "
                f"{(r.stderr or '').strip()[-300:]})"]
    try:
        rules = json.loads(r.stdout)["PacketFilterRules"] or []
        if any(_allows_ssh(rule, src, dst) for rule in rules for src, dst in pairs):
            return []
        allows = [_describe(rule) for rule in rules]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return [f"could not parse the tailnet policy this server received "
                f"from `tailscale debug netmap` ({type(e).__name__}: {e})"]
    tags = f" ({', '.join(sorted(own_tags))})" if own_tags else ""
    return [f"the tailnet policy lets none of "
            f"{', '.join(n for n, _ in peers[:MAX_PEERS])} open SSH (TCP port "
            f"22) to this server{tags}: it lets in "
            f"{_brief(allows, 5) or 'nothing'}"]


def check(cfg: dict, run: Run = _run, http: Http = _http,
          sleep: Callable[[float], None] = time.sleep) -> dict:
    provider = (cfg.get("TAILNET_PROVIDER") or "tailscale").strip().lower()
    require_peer = (cfg.get("TAILNET_REQUIRE_PEER") or "0").strip() == "1"
    own_tags = {t.strip() for t in (cfg.get("TAILNET_OWN_TAGS") or "").split(",")
                if t.strip()}

    reasons: list[str] = []
    notes: list[str] = []
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(DELAY_S)
        reasons, notes = [], []
        try:
            status = local_status(run)
        except (RuntimeError, ValueError) as e:
            reasons = [str(e)]
            continue
        if status.get("BackendState") != "Running":
            reasons = [f"tailscale is {status.get('BackendState')!r}, not Running"]
            continue

        if provider == "headscale":
            control = headscale_control(status, cfg, http)
            if control is None:
                if require_peer:
                    reasons.append(
                        "no Headscale API key is stored, so the control server "
                        "cannot confirm this server is online; enter one in "
                        "catena-admin > Settings > Admin access tunnel")
                else:
                    notes.append("Headscale not asked (no API key stored)")
            else:
                reasons += control
        else:
            reasons += tailscale_control(status, cfg, http)

        if require_peer and not reasons:
            peers = eligible_peers(status, own_tags)
            if not peers:
                reasons.append(
                    "no other device is online on the tailnet: turn Tailscale "
                    "on on the device used to administer this server, then "
                    "apply again")
            else:
                answered = ping_a_peer(peers, run)
                if answered:
                    notes.append(f"{answered} answered through the tunnel")
                else:
                    reasons.append(
                        f"none of {', '.join(n for n, _ in peers[:MAX_PEERS])} "
                        f"answered a ping through the tunnel")
        if not reasons:
            break

    # Once, after the tailnet answered: a policy or a firewall rule is not
    # something a wait clears.
    if require_peer and not reasons:
        reasons += policy_reasons(status, peers, own_tags, run)
        reasons += firewall_reasons(status, run)

    ok = not reasons
    summary = ("the tailnet confirms this server is reachable"
               + (f": {'; '.join(notes)}" if notes else "")) if ok else \
        "; ".join(reasons)
    return {"ok": ok, "reasons": reasons, "summary": summary}


def main() -> int:
    print(json.dumps(check(json.load(sys.stdin))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
