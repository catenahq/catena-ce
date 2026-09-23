"""An install that reports finished and does not work is the worst outcome.

Worse than one that stops and explains why: a client who is told their server
is ready acts on it. So each step passes only when the thing it is about has
been OBSERVED working, and a step that cannot observe says so rather than
assuming.

The domain is where that distinction is concrete. A zone waiting on its
registrar's nameservers looks entirely correct -- the token is valid, the zone
exists, the API accepts every record -- and nothing it publishes resolves. An
install on one issues no certificate, publishes a wildcard nobody can follow,
and then waits forever on an address that never answers.

Run: uv run pytest tests/test_no_step_reports_success_for_a_host_that_does_not_work.py
"""
from __future__ import annotations

import pytest

from catena_gui import steps as steps_mod


@pytest.fixture
def cf(monkeypatch):
    """Script the two Cloudflare calls, in the order the probe makes them."""
    calls: list[str] = []

    def scripted(verify, zones):
        def fake(url, *, headers=None, data=None, timeout=10.0):
            calls.append(url)
            return verify if "tokens/verify" in url else zones
        monkeypatch.setattr(steps_mod, "_http_json", fake)
        return calls
    return scripted


_LIVE = (200, {"result": {"status": "active"}})


def _blocking(checks):
    return [c for c in checks if c.blocks]


# --- the domain --------------------------------------------------------------

def test_an_active_zone_and_a_live_token_pass(cf):
    cf(_LIVE, (200, {"result": [{"id": "z1", "status": "active"}]}))
    checks = steps_mod.check_domain({"CLOUDFLARE_ZONE": "client.test"},
                                    {"cloudflare_api_token": "tok"})
    assert not _blocking(checks)


def test_a_pending_zone_blocks_and_names_the_nameservers(cf):
    """The remedy has to be on screen. A client whose registrar has not
    delegated the domain can act on "point it at these two names"; they cannot
    act on "the install failed"."""
    cf(_LIVE, (200, {"result": [{"id": "z1", "status": "pending",
                                 "name_servers": ["gail.ns.cloudflare.com",
                                                  "hugh.ns.cloudflare.com"]}]}))
    checks = steps_mod.check_domain({"CLOUDFLARE_ZONE": "client.test"},
                                    {"cloudflare_api_token": "tok"})
    blocked = _blocking(checks)
    assert blocked, "a pending zone advanced"
    assert "gail.ns.cloudflare.com" in blocked[0].detail


def test_a_dead_token_blocks_before_the_zone_is_asked_about(cf):
    """Stopping here means the second call is never made with a credential
    already known to be dead."""
    calls = cf((200, {"result": {"status": "expired"}}), (200, {}))
    assert _blocking(steps_mod.check_domain({"CLOUDFLARE_ZONE": "c.test"},
                                            {"cloudflare_api_token": "tok"}))
    assert all("tokens/verify" in c for c in calls)


def test_a_token_scoped_elsewhere_blocks(cf):
    """The shape a copy-paste from another account takes: valid, and granting
    nothing on the domain this server is about to serve."""
    cf(_LIVE, (200, {"result": []}))
    blocked = _blocking(steps_mod.check_domain({"CLOUDFLARE_ZONE": "c.test"},
                                               {"cloudflare_api_token": "tok"}))
    assert blocked and "Zone Resources" in blocked[0].detail


def test_no_domain_and_no_token_is_a_supported_install(cf):
    """A server installed before its domain is decided is the product's own
    case, not a degraded one. No call is made at all."""
    calls = cf(_LIVE, (200, {"result": []}))
    checks = steps_mod.check_domain({"CLOUDFLARE_ZONE": ""}, {})
    assert not _blocking(checks)
    assert calls == []


def test_a_domain_with_no_token_blocks(cf):
    """The contradiction. The server cannot create a single name under a
    domain it has no credential for, so an install would finish with a domain
    that publishes nothing and no message saying why."""
    cf(_LIVE, (200, {"result": []}))
    assert _blocking(steps_mod.check_domain({"CLOUDFLARE_ZONE": "c.test"}, {}))


# --- the access plane --------------------------------------------------------

def test_no_private_network_passes_and_says_the_port_stays_open(monkeypatch):
    """A posture rather than a failure. The installer will not close the port,
    and the client should read that here rather than discover it later."""
    checks = steps_mod.check_access({"ACCESS_METHOD": "public_ssh"}, {})
    assert not _blocking(checks)
    assert "keeps its SSH port open" in checks[0].detail


def test_a_tailnet_with_no_credential_blocks(monkeypatch):
    checks = steps_mod.check_access(
        {"ACCESS_METHOD": "tailnet", "TAILNET_PROVIDER": "tailscale"}, {})
    assert _blocking(checks)


def test_the_oauth_pair_is_exchanged_rather_than_taken_on_trust(monkeypatch):
    """A pair that looks right and does not exchange produces a server that
    never joins -- discovered partway through a bootstrap, after the client
    has walked away."""
    monkeypatch.setattr(steps_mod, "_http_json",
                        lambda *a, **k: (401, {"error": "invalid_client"}))
    checks = steps_mod.check_access(
        {"ACCESS_METHOD": "tailnet", "TAILNET_PROVIDER": "tailscale"},
        {"tailscale_oauth_client_id": "x", "tailscale_oauth_client_secret": "y"})
    assert _blocking(checks)

    monkeypatch.setattr(steps_mod, "_http_json",
                        lambda *a, **k: (200, {"access_token": "t"}))
    checks = steps_mod.check_access(
        {"ACCESS_METHOD": "tailnet", "TAILNET_PROVIDER": "tailscale"},
        {"tailscale_oauth_client_id": "x", "tailscale_oauth_client_secret": "y"})
    assert not _blocking(checks)


def test_headscale_needs_an_address_and_one_of_its_two_keys(monkeypatch):
    monkeypatch.setattr(steps_mod, "_http_json", lambda *a, **k: (200, {}))
    answers = {"ACCESS_METHOD": "tailnet", "TAILNET_PROVIDER": "headscale",
               "TAILNET_CONTROL_URL": "https://hs.example.net"}
    assert _blocking(steps_mod.check_access(answers, {}))
    assert not _blocking(steps_mod.check_access(answers, {"headscale_api_key": "k"}))
    assert not _blocking(
        steps_mod.check_access(answers, {"headscale_preauth_key": "k"}))


# --- the target --------------------------------------------------------------

def test_a_missing_keypair_does_not_block(monkeypatch):
    """The installer offers to generate it, so refusing here would block an
    install on a file that is one prompt away."""
    monkeypatch.setattr(steps_mod, "_tcp_open", lambda *a, **k: True)
    checks = steps_mod.check_target({"HOST_PUBLIC_IP": "203.0.113.10",
                                     "SSH_PRIVATE_KEY": "/nope/id",
                                     "SSH_PUBLIC_KEY_FILE": "/nope/id.pub"})
    assert not _blocking(checks)
    assert any(not c.ok for c in checks), "the absence is not even reported"


def test_a_server_that_does_not_answer_blocks(monkeypatch):
    """A server still being delivered is the ordinary reason, and this page is
    where a run waits for it."""
    monkeypatch.setattr(steps_mod, "_tcp_open", lambda *a, **k: False)
    assert _blocking(steps_mod.check_target({"HOST_PUBLIC_IP": "203.0.113.10"}))


# --- the keyset --------------------------------------------------------------

def test_the_keyset_acknowledgement_cannot_be_skipped():
    """`catena install` shows three passwords once and nothing off the server
    holds a copy. Without this the launcher ships installs nobody can
    recover."""
    assert _blocking(steps_mod.check_keyset({}))
    assert not _blocking(steps_mod.check_keyset({"_keyset_acknowledged": "yes"}))


def test_the_keyset_is_the_last_step():
    """Terminal as well as mandatory. A step after it would be one a client
    could still be on when the install started."""
    from catena_gui import registry

    doc = registry.load()
    assert registry.steps(doc)[-1]["name"] == "keyset"
