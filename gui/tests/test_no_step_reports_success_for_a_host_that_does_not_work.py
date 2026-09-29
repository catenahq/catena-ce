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


def test_a_token_with_no_domain_asks_for_one(cf):
    """All or nothing, in the other direction: the domains the token reaches
    are offered in the list, and one has to be picked."""
    cf(_LIVE, (200, {"result": [{"name": "b.test"}, {"name": "a.test"}]}))
    blocked = _blocking(steps_mod.check_domain({"CLOUDFLARE_ZONE": ""},
                                               {"cloudflare_api_token": "tok"}))
    assert blocked and "pick" in blocked[0].detail
    assert steps_mod.zone_choices({"cloudflare_api_token": "tok"}) == ["a.test", "b.test"]


def test_a_domain_with_no_token_blocks(cf):
    """The contradiction. The server cannot create a single name under a
    domain it has no credential for, so an install would finish with a domain
    that publishes nothing and no message saying why."""
    cf(_LIVE, (200, {"result": []}))
    assert _blocking(steps_mod.check_domain({"CLOUDFLARE_ZONE": "c.test"}, {}))


# --- the private network ----------------------------------------------------

def test_no_private_network_is_a_supported_install():
    """SSH on the public address stays the way in, so a server with no tailnet
    is a posture rather than a failure, whatever the domain section says."""
    for answers, secrets in (
        ({}, {}),
        ({"TAILNET_PROVIDER": "tailscale"}, {}),
        ({"TAILNET_PROVIDER": "headscale"}, {}),
    ):
        checks = steps_mod.check_access(answers, secrets)
        assert not _blocking(checks), (answers, secrets)
        assert "SSH" in checks[0].detail


def test_half_a_tailscale_credential_blocks():
    """All or nothing: an id with no secret describes no tailnet."""
    for secrets in ({"tailscale_oauth_client_id": "x"},
                    {"tailscale_oauth_client_secret": "y"}):
        assert _blocking(steps_mod.check_access({"TAILNET_PROVIDER": "tailscale"},
                                                secrets))


def test_the_oauth_pair_is_exchanged_rather_than_taken_on_trust(monkeypatch):
    """A pair that looks right and does not exchange produces a server that
    never joins -- discovered partway through a bootstrap, after the client
    has walked away."""
    monkeypatch.setattr(steps_mod, "_http_json",
                        lambda *a, **k: (401, {"error": "invalid_client"}))
    checks = steps_mod.check_access(
        {"TAILNET_PROVIDER": "tailscale"},
        {"tailscale_oauth_client_id": "x", "tailscale_oauth_client_secret": "y"})
    assert _blocking(checks)

    monkeypatch.setattr(steps_mod, "_http_json",
                        lambda *a, **k: (200, {"access_token": "t"}))
    checks = steps_mod.check_access(
        {"TAILNET_PROVIDER": "tailscale"},
        {"tailscale_oauth_client_id": "x", "tailscale_oauth_client_secret": "y"})
    assert not _blocking(checks)


def test_an_oauth_client_that_cannot_read_devices_blocks(monkeypatch):
    """The lockdown asks the control server whether the server is connected
    before it closes public SSH. A client without the scope would install and
    then refuse at its last step, so the page names the scope now."""
    def fake(url, *, headers=None, data=None, timeout=10.0):
        return (403, {}) if url.endswith("/devices") else (200, {"access_token": "t"})
    monkeypatch.setattr(steps_mod, "_http_json", fake)
    blocked = _blocking(steps_mod.check_access(
        {"TAILNET_PROVIDER": "tailscale"},
        {"tailscale_oauth_client_id": "x", "tailscale_oauth_client_secret": "y"}))
    assert blocked and "Devices > Core (read)" in blocked[0].detail


def test_a_headscale_api_key_is_tried_against_the_node_api(monkeypatch):
    """The same call the lockdown makes. A key the control server refuses is
    refused here, before the install."""
    seen = []
    state = {"status": 200}

    def fake(url, *, headers=None, data=None, timeout=10.0):
        seen.append((url, (headers or {}).get("Authorization")))
        return state["status"], {}
    monkeypatch.setattr(steps_mod, "_http_json", fake)
    answers = {"TAILNET_PROVIDER": "headscale",
               "TAILNET_CONTROL_URL": "https://hs.example.net/"}
    assert not _blocking(steps_mod.check_access(answers, {"headscale_api_key": "k"}))
    assert seen[-1] == ("https://hs.example.net/api/v1/node", "Bearer k")
    state["status"] = 401
    assert _blocking(steps_mod.check_access(answers, {"headscale_api_key": "k"}))


def test_a_headscale_preauth_key_alone_warns_about_the_panel(monkeypatch):
    """A pre-authentication key cannot be tested without using it up, and the
    panel's later lockdown needs the API key to ask Headscale."""
    monkeypatch.setattr(steps_mod, "_http_json", lambda *a, **k: (200, {}))
    answers = {"TAILNET_PROVIDER": "headscale",
               "TAILNET_CONTROL_URL": "https://hs.example.net"}
    checks = steps_mod.check_access(answers, {"headscale_preauth_key": "k"})
    warning = [c for c in checks if not c.blocking]
    assert warning and not _blocking(checks)
    assert "panel" in warning[0].detail
    monkeypatch.setattr(steps_mod, "_http_json", lambda *a, **k: (0, {}))
    assert _blocking(steps_mod.check_access(answers, {"headscale_preauth_key": "k"}))


def test_headscale_needs_an_address_and_one_of_its_two_keys(monkeypatch):
    monkeypatch.setattr(steps_mod, "_http_json", lambda *a, **k: (200, {}))
    answers = {"TAILNET_PROVIDER": "headscale",
               "TAILNET_CONTROL_URL": "https://hs.example.net"}
    assert _blocking(steps_mod.check_access(answers, {}))
    assert _blocking(steps_mod.check_access({"TAILNET_PROVIDER": "headscale"},
                                            {"headscale_api_key": "k"}))
    assert not _blocking(steps_mod.check_access(answers, {"headscale_api_key": "k"}))


def test_the_access_method_follows_from_the_credentials():
    """Asked separately it could be `tailnet` with no credential, or
    `public_ssh` beside one."""
    derive = steps_mod.derive
    assert derive({}, {})["ACCESS_METHOD"] == "public_ssh"
    assert derive({}, {"tailscale_oauth_client_id": "x"})["ACCESS_METHOD"] == "public_ssh"
    assert derive({}, {"tailscale_oauth_client_id": "x",
                       "tailscale_oauth_client_secret": "y"})["ACCESS_METHOD"] == "tailnet"
    hs = {"TAILNET_PROVIDER": "headscale", "TAILNET_CONTROL_URL": "https://hs"}
    assert derive(hs, {})["ACCESS_METHOD"] == "public_ssh"
    assert derive(hs, {"headscale_preauth_key": "k"})["ACCESS_METHOD"] == "tailnet"


# --- the target --------------------------------------------------------------

def _keypair(tmp_path):
    key = tmp_path / "id"
    key.write_text("k")
    (tmp_path / "id.pub").write_text("p")
    return {"HOST_PUBLIC_IP": "203.0.113.10", "SSH_PRIVATE_KEY": str(key),
            "SSH_PUBLIC_KEY_FILE": str(key) + ".pub"}


def test_a_server_that_does_not_speak_ssh_blocks(monkeypatch, tmp_path):
    """A connection is not an SSH server. A server still being delivered is the
    ordinary reason nothing answers, and this page is where a run waits."""
    for banner in ("", "HTTP/1.1 400 Bad Request"):
        monkeypatch.setattr(steps_mod, "_ssh_banner", lambda *a, b=banner, **k: b)
        assert _blocking(steps_mod.check_target(_keypair(tmp_path)))


def test_a_missing_keypair_blocks(monkeypatch):
    """The graphical installer has no password path: a key the server has
    never seen cannot log in, so there is nothing to generate here."""
    monkeypatch.setattr(steps_mod, "_ssh_banner", lambda *a, **k: "SSH-2.0-OpenSSH")
    blocked = _blocking(steps_mod.check_target({"HOST_PUBLIC_IP": "203.0.113.10",
                                                "SSH_PRIVATE_KEY": "/nope/id",
                                                "SSH_PUBLIC_KEY_FILE": "/nope/id.pub"}))
    assert blocked and "ssh-keygen" in blocked[0].detail


def test_the_key_has_to_log_in(monkeypatch, tmp_path):
    """The initial login, or ops on a server a previous run already hardened
    (root refused by then). Either one is a server this install can reach."""
    monkeypatch.setattr(steps_mod, "_ssh_banner", lambda *a, **k: "SSH-2.0-OpenSSH")
    answers = {**_keypair(tmp_path), "HOST_INITIAL_USER": "debian"}
    tried = []
    accepts: set[str] = set()

    def login(host, port, user, key):
        tried.append(user)
        return user in accepts, "Permission denied (publickey)"
    monkeypatch.setattr(steps_mod, "_ssh_login", login)

    accepts.add("debian")
    assert not _blocking(steps_mod.check_target(answers))
    accepts.clear()
    accepts.add("ops")
    assert not _blocking(steps_mod.check_target(answers))
    accepts.clear()
    tried.clear()
    blocked = _blocking(steps_mod.check_target(answers))
    assert blocked and tried == ["debian", "ops"]
    assert "Permission denied" in blocked[0].detail


def test_a_required_field_left_empty_blocks_before_any_probe():
    from catena_gui import registry

    doc = registry.load()
    target = next(s for s in steps_mod.build(doc) if s.name == "target")
    missing = steps_mod.missing_required(target, {})
    assert missing and all(c.blocks for c in missing)
    names = {f.key for f in target.fields if not f.optional}
    assert {c.label.split()[0] for c in missing} == names


# --- the backup -------------------------------------------------------------

_REPO = {"BACKUP_RESTIC_REPO": "s3:https://s3.example.net/client-backups"}
_KEYS = {"backup_s3_access_key": "AK", "backup_s3_secret_key": "SK"}


def test_no_backup_is_a_supported_install():
    assert not _blocking(steps_mod.check_backup({}, {}))


def test_half_a_backup_blocks():
    for answers, secrets in ((_REPO, {}), ({}, _KEYS),
                             (_REPO, {"backup_s3_access_key": "AK"})):
        assert _blocking(steps_mod.check_backup(answers, secrets)), (answers, secrets)


def test_the_keys_have_to_open_the_bucket(monkeypatch):
    """A signed request, the way restic's own are signed. An endpoint that only
    answers proves nothing about the keys."""
    seen = []
    state = {"status": 200}

    def head(scheme, host, bucket, access, secret, region):
        seen.append((scheme, host, bucket, region))
        return state["status"], {}
    monkeypatch.setattr(steps_mod, "_sigv4_head_bucket", head)
    assert not _blocking(steps_mod.check_backup(_REPO, _KEYS))
    assert seen[-1] == ("https", "s3.example.net", "client-backups", "us-east-1")
    state["status"] = 403
    blocked = _blocking(steps_mod.check_backup(_REPO, _KEYS))
    assert blocked and "refused these keys" in blocked[0].detail


def test_a_regional_store_is_asked_again_in_its_own_region(monkeypatch):
    regions = []

    def head(scheme, host, bucket, access, secret, region):
        regions.append(region)
        if region == "us-east-1":
            return 301, {"x-amz-bucket-region": "eu-west-3"}
        return 200, {}
    monkeypatch.setattr(steps_mod, "_sigv4_head_bucket", head)
    assert not _blocking(steps_mod.check_backup(_REPO, _KEYS))
    assert regions == ["us-east-1", "eu-west-3"]


# --- the access page ---------------------------------------------------------

def test_ssh_is_always_a_way_in(tmp_path):
    ways = steps_mod.ways_in({"HOST_PUBLIC_IP": "203.0.113.10",
                              "SSH_PRIVATE_KEY": "~/.ssh/k"}, {}, tmp_path)
    assert [w.title for w in ways] == ["SSH"]
    assert ways[0].commands == [
        "ssh -N -L 9010:127.0.0.1:9010 -L 9000:127.0.0.1:9000 -i ~/.ssh/k "
        "panel@203.0.113.10"]


def test_the_tailnet_and_the_tunnel_appear_when_configured(tmp_path):
    (tmp_path / "hosts.yml").write_text(
        "all:\n  children:\n    vps:\n      hosts:\n"
        "        h1:\n          ansible_host: 100.64.0.9\n")
    ways = steps_mod.ways_in(
        {"HOST_PUBLIC_IP": "203.0.113.10", "CLOUDFLARE_ZONE": "client.test"},
        {"tailscale_oauth_client_id": "x", "tailscale_oauth_client_secret": "y",
         "cloudflare_api_token": "t"}, tmp_path)
    by = {w.title: w for w in ways}
    assert set(by) == {"SSH", "The tailnet", "Cloudflare"}
    assert by["The tailnet"].commands[0].endswith("panel@100.64.0.9")
    assert "https://dash.client.test" in by["Cloudflare"].urls


# --- the keyset --------------------------------------------------------------

def test_the_keyset_acknowledgement_cannot_be_skipped():
    """`catena-cli install` shows three passwords once and nothing off the server
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
