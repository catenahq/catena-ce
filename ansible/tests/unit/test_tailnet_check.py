"""Unit tests for the controller-side tailnet preflight (helpers/tailnet_check.py).

The failure this gate exists to stop: preflight's OAuth probe talks to
api.tailscale.com over the plain internet, so it passes from a machine that
has never run `tailscale up` -- and the install then bootstraps the VPS
successfully before dying at `site` against a 100.x address it cannot route to.

The gate has two strengths, and the tests below pin the difference. A
CONFIRMED wrong tailnet blocks: it is a fact, and nothing downstream catches
it early. An unreadable local Tailscale state does NOT block: a controller can
route to the tailnet through a subnet router with no tailscale binary of its
own, so an absent CLI is missing evidence rather than a verdict, and the
converge probes the real address as soon as the node has one.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import tailnet_check as tc  # noqa: E402


def _status(**over):
    base = {
        "BackendState": "Running",
        "MagicDNSSuffix": "tail1234.ts.net",
        "Self": {"TailscaleIPs": ["100.101.102.103", "fd7a:115c::1"]},
    }
    base.update(over)
    return json.dumps(base)


def _fake_run(stdout="", returncode=0, stderr=""):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return run


# --- local_state -------------------------------------------------------------
def test_missing_cli_is_not_installed(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    state = tc.local_state()
    assert not state.installed
    assert not state.running


def test_running_node_reports_its_ipv4_and_suffix(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(_status()))
    state = tc.local_state()
    assert state.running
    assert state.ipv4 == "100.101.102.103"       # IPv6 is not the address SSH uses
    assert state.magic_dns_suffix == "tail1234.ts.net"


def test_logged_out_node_is_not_running_despite_json(monkeypatch):
    """`tailscale status --json` exits nonzero when logged out but still
    prints usable JSON, so the return code is not the signal -- reading it
    instead would call a logged-out machine 'not installed'."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(
        _status(BackendState="NeedsLogin", Self={"TailscaleIPs": []}), returncode=1))
    state = tc.local_state()
    assert state.installed
    assert not state.running
    assert state.backend_state == "NeedsLogin"


def test_running_without_an_address_is_not_running(monkeypatch):
    """BackendState alone is not enough: a node mid-join reports Running with
    no address yet, and that address is exactly what `site` will dial."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(
        _status(Self={"TailscaleIPs": []})))
    assert not tc.local_state().running


def test_garbage_output_does_not_raise(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run("not json", 1, "boom"))
    state = tc.local_state()
    assert state.installed and not state.running and state.detail == "boom"


# --- membership --------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _devices(*addr_lists):
    return {"devices": [{"addresses": list(a)} for a in addr_lists]}


def test_membership_matches_this_machines_address(monkeypatch):
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(_devices(
                            ["100.64.0.9", "fd7a::9"], ["100.101.102.103"])))
    verdict, _ = tc.membership("100.101.102.103", "tok")
    assert verdict == tc.MEMBER


def test_membership_rejects_a_different_tailnet(monkeypatch):
    """The credentials mint keys for one tailnet, this machine is joined to
    another. Bootstrap would succeed and `site` would never reach the box."""
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(_devices(["100.64.0.9"])))
    verdict, detail = tc.membership("100.101.102.103", "tok")
    assert verdict == tc.NOT_MEMBER
    assert "DIFFERENT tailnet" in detail


def test_narrow_oauth_scope_is_unknown_not_failure(monkeypatch):
    """403 means the client is scoped `Auth Keys: Write` only -- the correct
    production scope. A check that cannot see is not a check that failed."""
    def boom(req, timeout=0):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(tc.urllib.request, "urlopen", boom)
    verdict, detail = tc.membership("100.101.102.103", "tok")
    assert verdict == tc.UNKNOWN
    assert "Auth Keys" in detail


def test_network_failure_is_unknown(monkeypatch):
    def boom(req, timeout=0):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(tc.urllib.request, "urlopen", boom)
    assert tc.membership("100.101.102.103", "tok")[0] == tc.UNKNOWN


def test_membership_without_a_token_is_unknown():
    assert tc.membership("100.101.102.103", "")[0] == tc.UNKNOWN


# --- check (the gate) --------------------------------------------------------
@pytest.fixture
def up(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(_status()))


def test_missing_cli_warns_but_does_not_block(monkeypatch):
    """An absent CLI is the common shape of "never joined", but a controller
    can reach the tailnet through a subnet router without one. Blocking here
    would refuse a supported setup on evidence that was never gathered."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    result = tc.check()
    assert not result.ok
    assert not result.blocking
    assert "install.sh" in result.remedy and "tailscale up" in result.remedy


def test_installed_but_down_warns_but_does_not_block(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(
        _status(BackendState="Stopped", Self={"TailscaleIPs": []})))
    result = tc.check()
    assert not result.ok
    assert not result.blocking
    assert "tailscale up" in result.remedy


def test_check_passes_when_up_and_scope_is_narrow(up, monkeypatch):
    """The production shape: joined, and the credential cannot prove which
    tailnet. Passing is correct -- the answerable question answered yes."""
    def forbidden(req, timeout=0):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(tc.urllib.request, "urlopen", forbidden)
    assert tc.check(token="tok").ok


def test_check_blocks_on_a_confirmed_wrong_tailnet(up, monkeypatch):
    """The one verdict that stops the install: this machine is joined, the
    credentials are readable, and the two name different tailnets."""
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(_devices(["100.64.0.9"])))
    result = tc.check(token="tok")
    assert not result.ok
    assert result.blocking
    assert result.remedy


def test_headscale_skips_the_tailscale_api(up, monkeypatch):
    """api.tailscale.com knows nothing about a self-hosted control server, so
    asking it would answer NOT_MEMBER about a perfectly good setup."""
    def never(req, timeout=0):
        raise AssertionError("called the Tailscale API on a Headscale inventory")

    monkeypatch.setattr(tc.urllib.request, "urlopen", never)
    result = tc.check(token="tok", control_url="https://hs.example.net")
    assert result.ok


def test_headscale_remedy_names_the_login_server(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    result = tc.check(control_url="https://hs.example.net")
    assert "--login-server https://hs.example.net" in result.remedy


def test_cli_exits_zero_and_still_prints_the_remedy_when_it_cannot_look(
        monkeypatch, capsys):
    """preflight.yml runs this as a task, so the exit code decides whether the
    play dies. A missing CLI prints the join instructions and exits 0 -- the
    operator is told, and a controller reaching the tailnet by another route
    is not refused."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    assert tc.main([]) == 0
    assert "https://tailscale.com/download" in capsys.readouterr().err


def test_cli_never_blocks_because_it_holds_no_token(monkeypatch):
    """The CLI cannot reach the one blocking verdict, and that is deliberate:
    the wrong-tailnet comparison needs an API token, and a bearer token on
    argv lands in `ps`. So the CLI form is a reporter -- it prints what it can
    see and exits 0 either way. The blocking gate lives in seed.py, which
    already holds the token it exchanged in-process.

    Pinned so a future `--token` flag has to confront this rather than
    quietly turning an advisory task into one that fails a play."""
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(_status()))
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(_devices(["100.64.0.9"])))
    assert tc.main([]) == 0


def test_cli_exits_zero_when_on_the_right_tailnet(monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(tc.subprocess, "run", _fake_run(_status()))
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(_devices(["100.101.102.103"])))
    assert tc.main([]) == 0


def test_cli_takes_no_token_argument():
    """A bearer token on argv lands in `ps` and in the printed command. Callers
    holding one call check() in-process instead."""
    with pytest.raises(SystemExit):
        tc.main(["--token", "tok"])
