"""The lockdown's question to the tailnet (helpers/tailnet_reachability.py).

On the host itself -- the panel's lockdown -- a TCP probe of the tailnet
address dials the host's own address and passes whatever the tailnet thinks.
So the host asks the control server (connected, not refusing incoming) and,
there, an online peer that is not another server like it (a TSMP ping through
the tunnel). The ping is answered inside tailscaled before its packet filter
runs, so the host also reads the filter its control server sent it for a rule
that lets such a peer open SSH, and walks its own firewall for SSH arriving on
tailscale0. These tests pin each refusal and what it tells the client.

Run: uv run pytest tests/unit/test_tailnet_reachability.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import tailnet_reachability as tr  # noqa: E402

SECRET = "tskey-client-SECRET"
NODE_KEY = "nodekey:abc"


def _status(*, backend="Running", peers=None):
    return {"BackendState": backend,
            "Self": {"ID": "nSELF", "PublicKey": NODE_KEY,
                     "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"]},
            "Peer": {f"k{i}": p for i, p in enumerate(peers or [])}}


# The shape a converged host carries: Docker's raw anti-spoofing rules,
# Tailscale's ts-input jumped first, ufw's tailnet SSH allow, policy DROP.
ACCEPTING = """\
*raw
:PREROUTING ACCEPT [0:0]
-A PREROUTING -d 172.18.0.2/32 ! -i docker_gwbridge -j DROP
COMMIT
*filter
:INPUT DROP [0:0]
:ts-input - [0:0]
:ufw-before-input - [0:0]
:ufw-user-input - [0:0]
{first}-A INPUT -j ts-input
-A INPUT -j ufw-before-input
-A ts-input -i tailscale0 -j ACCEPT
-A ts-input -s 100.64.0.0/10 ! -i tailscale0 -j DROP
-A ufw-before-input -m conntrack --ctstate INVALID -j DROP
-A ufw-before-input -j ufw-user-input
-A ufw-user-input -i tailscale0 -p tcp -m tcp --dport 22 -j ACCEPT
COMMIT
"""
# The bench's tailnet partition, jumped ahead of everything.
PARTITION = ("-A INPUT -j CATENA_BENCH_FI\n"
             "-A CATENA_BENCH_FI -i tailscale0 -m comment --comment "
             "fi-partition -j DROP\n")


def _ruleset(first=""):
    return ACCEPTING.replace("{first}", first)


def _peer(name, *, online=True, tags=None, ip="100.64.0.9"):
    return {"HostName": name, "Online": online, "Tags": tags or [],
            "TailscaleIPs": [ip, "fd7a::9"]}


# tailcfg.FilterAllowAll: what a control server with no policy sends.
ALLOW_ALL = [{"SrcIPs": ["*"],
              "DstPorts": [{"IP": "*", "Ports": {"First": 0, "Last": 65535}}]}]


def _rule(src, dst, first=22, last=None, protos=None):
    rule = {"SrcIPs": [src],
            "DstPorts": [{"IP": dst, "Ports": {"First": first,
                                               "Last": first if last is None else last}}]}
    if protos is not None:
        rule["IPProto"] = protos
    return rule


class _World:
    """A host's `tailscale` CLI and the control server's API, faked."""

    def __init__(self, status, *, device=None, device_code=200, nodes=None,
                 answering=(), rulesets=None, rules=ALLOW_ALL, netmap=None):
        self.status = status
        self.device = device if device is not None else {
            "connectedToControl": True, "blocksIncomingConnections": False}
        self.device_code = device_code
        self.nodes = nodes
        self.answering = set(answering)
        self.rulesets = rulesets if rulesets is not None else {
            "iptables-save": _ruleset(), "ip6tables-save": _ruleset()}
        # `tailscale debug netmap` as (rc, stdout); the default carries `rules`.
        self.netmap = netmap or (0, json.dumps({"PacketFilterRules": rules}))
        self.argvs: list[list[str]] = []
        self.urls: list[str] = []

    def run(self, argv):
        self.argvs.append(argv)
        if argv[:2] == ["tailscale", "status"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.status), "")
        if argv[:2] == ["tailscale", "ping"]:
            rc = 0 if argv[-1] in self.answering else 1
            return subprocess.CompletedProcess(argv, rc, "", "no reply")
        if argv == ["tailscale", "debug", "netmap"]:
            rc, out = self.netmap
            return subprocess.CompletedProcess(argv, rc, out, "" if rc == 0 else "no netmap")
        if argv[0] in self.rulesets:
            return subprocess.CompletedProcess(argv, 0, self.rulesets[argv[0]], "")
        if argv[0].endswith("tables-save"):
            raise FileNotFoundError(argv[0])
        raise AssertionError(argv)

    def http(self, method, url, *, headers=None, data=None):
        self.urls.append(url)
        if url.endswith("/oauth/token"):
            return 200, {"access_token": "bearer"}
        if "/device/" in url:
            return self.device_code, self.device if self.device_code == 200 else None
        if url.endswith("/api/v1/node"):
            return 200, {"nodes": self.nodes or []}
        raise AssertionError(url)


def _env(**over):
    env = {"TAILNET_PROVIDER": "tailscale",
           "TAILSCALE_API_BASE": "https://api.test/api/v2",
           "TAILSCALE_OAUTH_CLIENT_ID": "id",
           "TAILSCALE_OAUTH_CLIENT_SECRET": SECRET,
           "TAILNET_OWN_TAGS": "tag:vps", "TAILNET_REQUIRE_PEER": "0"}
    env.update(over)
    return env


def _check(world, **env):
    return tr.check(_env(**env), run=world.run, http=world.http,
                    sleep=lambda s: None)


def test_from_the_installer_a_connected_accepting_node_passes() -> None:
    world = _World(_status())
    verdict = _check(world)
    assert verdict["ok"], verdict
    assert any(u.endswith("/device/nSELF?fields=all") for u in world.urls)
    assert not any(a[:2] == ["tailscale", "ping"] for a in world.argvs), (
        "the controller's own TCP probe is the peer proof on this path")


def test_a_node_the_control_server_does_not_see_is_refused() -> None:
    world = _World(_status(), device={"connectedToControl": False})
    verdict = _check(world)
    assert not verdict["ok"]
    assert "does not see this server connected" in verdict["summary"]


def test_shields_up_is_refused() -> None:
    world = _World(_status(), device={"connectedToControl": True,
                                      "blocksIncomingConnections": True})
    verdict = _check(world)
    assert not verdict["ok"]
    assert "refuses incoming" in verdict["summary"]


def test_a_client_without_the_device_scope_is_told_which_scope() -> None:
    verdict = _check(_World(_status(), device_code=403))
    assert not verdict["ok"]
    assert "Devices > Core (read)" in verdict["summary"]


def test_a_node_not_running_is_refused() -> None:
    verdict = _check(_World(_status(backend="NeedsLogin")))
    assert not verdict["ok"]
    assert "NeedsLogin" in verdict["summary"]


def test_on_the_host_a_peer_must_answer_through_the_tunnel() -> None:
    world = _World(_status(peers=[_peer("laptop", ip="100.64.0.7")]),
                   answering={"100.64.0.7"})
    verdict = _check(world, TAILNET_REQUIRE_PEER="1")
    assert verdict["ok"], verdict
    assert "laptop answered" in verdict["summary"]
    ping = next(a for a in world.argvs if a[:2] == ["tailscale", "ping"])
    assert "--tsmp" in ping and ping[-1] == "100.64.0.7"


def test_on_the_host_no_other_device_online_is_refused() -> None:
    peers = [_peer("laptop", online=False),
             _peer("other-server", tags=["tag:vps"])]
    verdict = _check(_World(_status(peers=peers)), TAILNET_REQUIRE_PEER="1")
    assert not verdict["ok"]
    assert "no other device is online" in verdict["summary"]


def test_on_the_host_a_peer_that_never_answers_is_refused() -> None:
    world = _World(_status(peers=[_peer("laptop")]))
    verdict = _check(world, TAILNET_REQUIRE_PEER="1")
    assert not verdict["ok"]
    assert "laptop" in verdict["summary"] and "answered" in verdict["summary"]


def test_headscale_online_node_passes_and_offline_is_refused() -> None:
    online = _World(_status(), nodes=[{"nodeKey": NODE_KEY, "online": True}])
    assert _check(online, TAILNET_PROVIDER="headscale",
                  HEADSCALE_URL="https://hs.test", HEADSCALE_API_KEY="k")["ok"]
    offline = _World(_status(), nodes=[{"nodeKey": NODE_KEY, "online": False}])
    verdict = _check(offline, TAILNET_PROVIDER="headscale",
                     HEADSCALE_URL="https://hs.test", HEADSCALE_API_KEY="k")
    assert not verdict["ok"] and "online" in verdict["summary"]


def test_headscale_without_an_api_key_passes_only_from_the_installer() -> None:
    world = _World(_status(peers=[_peer("laptop")]), answering={"100.64.0.9"})
    installer = _check(world, TAILNET_PROVIDER="headscale")
    assert installer["ok"] and "not asked" in installer["summary"]
    on_host = _check(world, TAILNET_PROVIDER="headscale", TAILNET_REQUIRE_PEER="1")
    assert not on_host["ok"]
    assert "Headscale API key" in on_host["summary"]


def test_the_verdict_never_carries_a_credential() -> None:
    for world in (_World(_status()), _World(_status(), device_code=403)):
        assert SECRET not in json.dumps(_check(world))


def test_a_transient_miss_is_waited_out() -> None:
    """A node that just ran `tailscale up` reaches the control server a few
    seconds later."""
    world = _World(_status(), device={"connectedToControl": False})
    calls = {"n": 0}

    def http(method, url, **kw):
        if "/device/" in url:
            calls["n"] += 1
            return 200, {"connectedToControl": calls["n"] >= 3}
        return world.http(method, url, **kw)
    verdict = tr.check(_env(), run=world.run, http=http, sleep=lambda s: None)
    assert verdict["ok"] and calls["n"] == 3


# --- the tailnet policy -----------------------------------------------------

def _under_policy(**world):
    peers = [_peer("laptop", ip="100.64.0.7"),
             _peer("other-server", tags=["tag:vps"], ip="100.64.0.50")]
    w = _World(_status(peers=peers), answering={"100.64.0.7"}, **world)
    return _check(w, TAILNET_REQUIRE_PEER="1"), w


def test_an_allow_all_policy_passes() -> None:
    verdict, world = _under_policy()
    assert verdict["ok"], verdict
    assert ["tailscale", "debug", "netmap"] in world.argvs


def test_a_policy_keeping_ssh_from_the_server_is_refused_and_named() -> None:
    """The ping still answers and the control server still sees the node
    online: only the filter says SSH would not get in."""
    verdict, _ = _under_policy(rules=[_rule("100.64.0.7", "100.64.0.2", 443)])
    assert not verdict["ok"]
    assert "lets none of laptop open SSH (TCP port 22) to this server (tag:vps)" \
        in verdict["summary"]
    assert "[tcp/udp/icmp from 100.64.0.7 to 100.64.0.2:443]" in verdict["summary"]


@pytest.mark.parametrize("rules, words", [
    # Only the other server, which carries this host's tag, may open 22.
    ([_rule("100.64.0.50", "100.64.0.2")], "from 100.64.0.50 to 100.64.0.2:22"),
    ([_rule("*", "100.64.0.99")], "to 100.64.0.99:22"),
    ([_rule("*", "*", protos=[17])], "[udp from * to *:22]"),
    ([{"SrcIPs": ["*"], "CapGrant": [{"Dsts": ["100.64.0.2/32"],
                                       "CapMap": {"tailscale.com/cap/drive": None}}]}],
     "[capability grants only, from *]"),
    ([_rule("cap:tailscale.com/cap/admin", "*")], "from cap:tailscale.com/cap/admin"),
    ([], "it lets in nothing"),
], ids=["source-no-eligible-peer-holds", "another-destination", "udp-only",
        "capability-grant-only", "capability-source", "empty"])
def test_a_policy_that_lets_no_eligible_peer_open_ssh_is_refused(rules, words) -> None:
    verdict, _ = _under_policy(rules=rules)
    assert not verdict["ok"], verdict
    assert "lets none of laptop open SSH" in verdict["summary"]
    assert words in verdict["summary"]


@pytest.mark.parametrize("rule", [
    _rule("100.64.0.7", "100.64.0.2", 20, 30),
    _rule("100.64.0.0/10", "100.64.0.2/32"),
    _rule("100.64.0.1-100.64.0.9", "100.64.0.2"),
    _rule("*", "*", protos=[6]),
    _rule("fd7a::9", "fd7a:115c:a1e0::2"),
], ids=["port-range", "cidr", "address-range", "tcp-listed", "ipv6"])
def test_a_rule_that_lets_an_eligible_peer_open_ssh_passes(rule) -> None:
    deny = _rule("*", "*", 443)
    verdict, _ = _under_policy(rules=[deny, rule])
    assert verdict["ok"], verdict


@pytest.mark.parametrize("netmap, words", [
    ((1, ""), "could not read the tailnet policy"),
    ((0, "not json"), "could not parse the tailnet policy"),
    ((0, "null"), "could not parse the tailnet policy"),
    ((0, json.dumps({"Peers": []})), "could not parse the tailnet policy"),
    ((0, json.dumps({"PacketFilterRules": [{"SrcIPs": ["*"], "DstPorts": [{"IP": "*"}]}]})),
     "could not parse the tailnet policy"),
], ids=["exit", "not-json", "no-netmap", "no-rules-field", "no-ports"])
def test_a_filter_that_cannot_be_read_is_refused(netmap, words) -> None:
    verdict, _ = _under_policy(netmap=netmap)
    assert not verdict["ok"], verdict
    assert words in verdict["summary"]


# --- the host's own firewall ------------------------------------------------

def _on_host(rulesets):
    world = _World(_status(peers=[_peer("laptop", ip="100.64.0.7")]),
                   answering={"100.64.0.7"}, rulesets=rulesets)
    return _check(world, TAILNET_REQUIRE_PEER="1"), world


def test_a_host_whose_firewall_lets_tailnet_ssh_in_passes() -> None:
    verdict, world = _on_host({"iptables-save": _ruleset(),
                               "ip6tables-save": _ruleset()})
    assert verdict["ok"], verdict
    assert ["iptables-save"] in world.argvs and ["ip6tables-save"] in world.argvs


def test_a_drop_ahead_of_tailscale_is_refused_and_named() -> None:
    """The bench's partition: the TSMP ping still answers, because tailscaled
    handles it without crossing tailscale0, and SSH would not get in."""
    verdict, _ = _on_host({"iptables-save": _ruleset(first=PARTITION),
                           "ip6tables-save": _ruleset()})
    assert not verdict["ok"]
    assert "firewall drops SSH arriving over the tailnet" in verdict["summary"]
    assert "fi-partition" in verdict["summary"]


def test_a_drop_behind_tailscales_own_accept_changes_nothing() -> None:
    late = _ruleset().replace(
        "-A INPUT -j ufw-before-input\n",
        "-A INPUT -j ufw-before-input\n" + PARTITION.replace(
            "-A INPUT -j CATENA_BENCH_FI\n", "") + "-A INPUT -j CATENA_BENCH_FI\n")
    verdict, _ = _on_host({"iptables-save": late, "ip6tables-save": _ruleset()})
    assert verdict["ok"], verdict


def test_without_tailscales_rules_ufw_decides() -> None:
    no_ts = _ruleset().replace("-A INPUT -j ts-input\n", "")
    allowed, _ = _on_host({"iptables-save": no_ts, "ip6tables-save": _ruleset()})
    assert allowed["ok"], allowed
    no_allow = no_ts.replace(
        "-A ufw-user-input -i tailscale0 -p tcp -m tcp --dport 22 -j ACCEPT\n", "")
    refused, _ = _on_host({"iptables-save": no_allow, "ip6tables-save": _ruleset()})
    assert not refused["ok"]
    assert "policy is DROP" in refused["summary"]


def test_a_drop_for_another_port_or_protocol_is_not_ssh() -> None:
    other = ("-A INPUT -j CATENA_BENCH_FI\n"
             "-A CATENA_BENCH_FI -i tailscale0 -p tcp -m tcp --dport 3306 -j DROP\n"
             "-A CATENA_BENCH_FI -i tailscale0 -p udp -j DROP\n")
    verdict, _ = _on_host({"iptables-save": _ruleset(first=other),
                           "ip6tables-save": _ruleset()})
    assert verdict["ok"], verdict


def test_a_drop_the_walk_cannot_decide_is_refused() -> None:
    """An undecidable condition never counts as letting SSH in."""
    limited = ("-A INPUT -j CATENA_BENCH_FI\n"
               "-A CATENA_BENCH_FI -i tailscale0 -m recent --update --seconds 30 "
               "--hitcount 6 -j REJECT --reject-with tcp-reset\n")
    verdict, _ = _on_host({"iptables-save": _ruleset(first=limited),
                           "ip6tables-save": _ruleset()})
    assert not verdict["ok"]
    assert "--hitcount 6" in verdict["summary"]


def test_the_ipv6_ruleset_is_walked_too() -> None:
    verdict, _ = _on_host({"iptables-save": _ruleset(),
                           "ip6tables-save": _ruleset(first=PARTITION)})
    assert not verdict["ok"]
    assert "ip6tables-save" in verdict["summary"]


def test_a_firewall_that_cannot_be_read_is_refused() -> None:
    verdict, _ = _on_host({"ip6tables-save": _ruleset()})
    assert not verdict["ok"]
    assert "could not read this server's firewall (iptables-save" in verdict["summary"]


def test_the_policy_and_firewall_are_read_only_on_the_host_and_after_the_tailnet() -> None:
    from_installer = _World(_status())
    assert _check(from_installer)["ok"]
    refused = _World(_status(), device={"connectedToControl": False})
    _check(refused, TAILNET_REQUIRE_PEER="1")
    for world in (from_installer, refused):
        assert not any(a[0].endswith("tables-save") for a in world.argvs)
        assert ["tailscale", "debug", "netmap"] not in world.argvs
