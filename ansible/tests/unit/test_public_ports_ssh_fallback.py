"""A host whose Lockdown closed public SSH reopens it while the tailnet is down.

The panel's Lockdown narrows port 22 to the private path after proving the
tailnet. The tailnet is a convenience, not the host's security boundary --
key-only SSH with no root and no password login is -- so a tailnet that stops
answering must not leave the host with no way in. The reconciler asks
tailscaled on every run and serves the narrowed declaration as `any` until the
tailnet is back; the declaration on disk never changes, so the port closes
again by itself.

Run: uv run pytest tests/unit/test_public_ports_ssh_fallback.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE_DIR / "scripts" / "catena-public-ports.py"

sys.path.insert(0, str(ANSIBLE_DIR / "helpers"))
import public_ports as pp  # noqa: E402


def _ssh(scope: str):
    return pp.normalize_infra([
        {"proto": "tcp", "port": 22, "scope": scope, "bind": "host",
         "owner": "ssh", "comment": "administrative SSH"},
    ])[0]


def _reconciler():
    spec = importlib.util.spec_from_file_location("catena_public_ports", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_locked_down_host_reopens_22_while_the_tailnet_is_down():
    out = pp.ssh_fallback([_ssh("private")], tailnet_up=False)
    assert [e.scope for e in out] == ["any"]
    assert pp.public_open_tcp_ports(out) == [22], (
        "validation reads the effective set; a reopened port has to be expected")


def test_it_closes_again_once_the_tailnet_is_back():
    entries = [_ssh("private")]
    assert pp.ssh_fallback(entries, tailnet_up=True) == entries


def test_only_the_ssh_declaration_is_reopened():
    """The fallback is about the way in. The panel's own port stays confined
    to the host whatever the tailnet does -- it is reached through SSH."""
    panel = pp.normalize_infra([
        {"proto": "tcp", "port": 9010, "scope": "loopback", "bind": "docker",
         "owner": "catena-admin"},
        {"proto": "tcp", "port": 9000, "scope": "private", "bind": "docker",
         "owner": "portainer"},
    ])
    out = pp.ssh_fallback(panel + [_ssh("private")], tailnet_up=False)
    scopes = {e.owner: e.scope for e in out}
    assert scopes == {"catena-admin": "loopback", "portainer": "private", "ssh": "any"}


def test_the_rule_plan_opens_the_public_rule_while_down():
    plan = pp.rule_plan(pp.ssh_fallback([_ssh("private")], tailnet_up=False),
                        tailnet_available=True)
    assert [(r["action"], r.get("from"), r.get("iface")) for r in plan] == [
        ("allow", "any", None)]


def _status(monkeypatch, mod, *, rc=0, body=None, raise_=None):
    def fake(argv, **kw):
        if raise_:
            raise raise_
        return subprocess.CompletedProcess(argv, rc, json.dumps(body), "")
    monkeypatch.setattr(mod.subprocess, "run", fake)


def test_tailnet_up_needs_a_running_online_backend_with_an_address(monkeypatch):
    mod = _reconciler()
    good = {"BackendState": "Running",
            "Self": {"Online": True, "TailscaleIPs": ["100.64.0.7", "fd7a::1"]}}
    _status(monkeypatch, mod, body=good)
    assert mod.tailnet_up()

    for bad in (
        {"BackendState": "NeedsLogin", "Self": good["Self"]},
        {"BackendState": "Running", "Self": {"Online": False,
                                             "TailscaleIPs": ["100.64.0.7"]}},
        {"BackendState": "Running", "Self": {"Online": True, "TailscaleIPs": []}},
        None,
    ):
        _status(monkeypatch, mod, body=bad)
        assert not mod.tailnet_up(), bad


def test_an_absent_or_hung_tailscaled_reads_as_down(monkeypatch):
    mod = _reconciler()
    _status(monkeypatch, mod, raise_=FileNotFoundError("tailscale"))
    assert not mod.tailnet_up()
    _status(monkeypatch, mod,
            raise_=subprocess.TimeoutExpired(["tailscale"], 15))
    assert not mod.tailnet_up()
    _status(monkeypatch, mod, rc=1, body={})
    assert not mod.tailnet_up()
