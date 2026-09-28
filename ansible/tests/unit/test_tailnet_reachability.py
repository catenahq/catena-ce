"""The lockdown's question to the tailnet (helpers/tailnet_reachability.py).

On the host itself -- the panel's lockdown -- a TCP probe of the tailnet
address dials the host's own address and passes whatever the tailnet thinks.
So the host asks the control server (connected, not refusing incoming) and,
there, an online peer that is not another server like it (a TSMP ping through
the tunnel). These tests pin each refusal and what it tells the client.

Run: uv run pytest tests/unit/test_tailnet_reachability.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import tailnet_reachability as tr  # noqa: E402

SECRET = "tskey-client-SECRET"
NODE_KEY = "nodekey:abc"


def _status(*, backend="Running", peers=None):
    return {"BackendState": backend,
            "Self": {"ID": "nSELF", "PublicKey": NODE_KEY},
            "Peer": {f"k{i}": p for i, p in enumerate(peers or [])}}


def _peer(name, *, online=True, tags=None, ip="100.64.0.9"):
    return {"HostName": name, "Online": online, "Tags": tags or [],
            "TailscaleIPs": [ip, "fd7a::9"]}


class _World:
    """A host's `tailscale` CLI and the control server's API, faked."""

    def __init__(self, status, *, device=None, device_code=200, nodes=None,
                 answering=()):
        self.status = status
        self.device = device if device is not None else {
            "connectedToControl": True, "blocksIncomingConnections": False}
        self.device_code = device_code
        self.nodes = nodes
        self.answering = set(answering)
        self.argvs: list[list[str]] = []
        self.urls: list[str] = []

    def run(self, argv):
        self.argvs.append(argv)
        if argv[:2] == ["tailscale", "status"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.status), "")
        if argv[:2] == ["tailscale", "ping"]:
            rc = 0 if argv[-1] in self.answering else 1
            return subprocess.CompletedProcess(argv, rc, "", "no reply")
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
