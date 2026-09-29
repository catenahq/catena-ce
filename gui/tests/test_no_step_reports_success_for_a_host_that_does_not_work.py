"""An install that reports finished and does not work is the worst outcome.

Worse than one that stops and explains why: a client who is told their server
is ready acts on it. So each step passes only when the thing it is about has
been OBSERVED working, and a step that cannot observe says so rather than
assuming.

The server is where that is concrete: an address that answers is not an SSH
server, and an SSH server is not one this install can log in to. The install
logs in with the key the provider installed, or with the provider's password,
once, to add it -- and the check says which, having tried it.

Run: uv run pytest tests/test_no_step_reports_success_for_a_host_that_does_not_work.py
"""
from __future__ import annotations

from catena_gui import registry, steps as steps_mod


def _blocking(checks):
    return [c for c in checks if c.blocks]


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
        assert _blocking(steps_mod.check_target(_keypair(tmp_path), {}))


def test_a_missing_keypair_blocks_and_says_how_to_make_one(monkeypatch):
    monkeypatch.setattr(steps_mod, "_ssh_banner", lambda *a, **k: "SSH-2.0-OpenSSH")
    blocked = _blocking(steps_mod.check_target({"HOST_PUBLIC_IP": "203.0.113.10",
                                                "SSH_PRIVATE_KEY": "/nope/id",
                                                "SSH_PUBLIC_KEY_FILE": "/nope/id.pub"},
                                               {}))
    assert blocked and "ssh-keygen" in blocked[0].detail


def _server(monkeypatch, *, key_opens=(), password_opens=None):
    """A server that speaks SSH, lets the key into `key_opens`, and answers the
    provider's password with `password_opens` (None: never asked)."""
    monkeypatch.setattr(steps_mod, "_ssh_banner", lambda *a, **k: "SSH-2.0-OpenSSH")
    tried: list[str] = []

    def login(host, port, user, key):
        tried.append(user)
        return user in key_opens, "Permission denied (publickey)"
    monkeypatch.setattr(steps_mod, "_ssh_login", login)

    asked: list[tuple] = []

    def password(host, port, user, pw):
        asked.append((user, pw))
        assert password_opens is not None, "the password was tried unasked"
        return password_opens, "" if password_opens else "debian refused the password"
    monkeypatch.setattr(steps_mod, "_password_login", password)
    return tried, asked


def test_a_key_the_provider_installed_opens_the_server(monkeypatch, tmp_path):
    """The provider-installed shape: no password is needed, and none is
    tried even when one was typed."""
    answers = {**_keypair(tmp_path), "HOST_INITIAL_USER": "debian"}
    _, asked = _server(monkeypatch, key_opens={"debian"})
    checks = steps_mod.check_target(answers, {steps_mod.PROVIDER_PASSWORD: "pw"})
    assert not _blocking(checks)
    assert "already opens debian@" in checks[-1].label
    assert asked == []


def test_a_server_a_previous_run_hardened_opens_as_ops(monkeypatch, tmp_path):
    """Root and the provider login are refused by then, and ops takes the key."""
    answers = {**_keypair(tmp_path), "HOST_INITIAL_USER": "debian"}
    tried, _ = _server(monkeypatch, key_opens={"ops"})
    assert not _blocking(steps_mod.check_target(answers, {}))
    assert tried == ["debian", "ops"]


def test_a_key_the_server_refuses_asks_for_the_provider_password(monkeypatch, tmp_path):
    """Blocking, and the remedy is on screen: the password, or the key given
    to the provider."""
    answers = {**_keypair(tmp_path), "HOST_INITIAL_USER": "debian"}
    _server(monkeypatch)
    blocked = _blocking(steps_mod.check_target(answers, {}))
    assert blocked and "provider's password for debian" in blocked[0].detail
    assert "Permission denied" in blocked[0].detail


def test_the_provider_password_is_tried_not_taken_on_trust(monkeypatch, tmp_path):
    answers = {**_keypair(tmp_path), "HOST_INITIAL_USER": "debian"}
    _, asked = _server(monkeypatch, password_opens=True)
    checks = steps_mod.check_target(answers, {steps_mod.PROVIDER_PASSWORD: "pw"})
    assert not _blocking(checks)
    assert asked == [("debian", "pw")]
    assert "adds the key" in checks[-1].detail

    _server(monkeypatch, password_opens=False)
    blocked = _blocking(steps_mod.check_target(
        answers, {steps_mod.PROVIDER_PASSWORD: "wrong"}))
    assert blocked and "refused" in blocked[0].detail


def test_a_required_field_left_empty_blocks_before_any_probe():
    doc = registry.load()
    target = next(s for s in steps_mod.build(doc) if s.name == "target")
    missing = steps_mod.missing_required(target, {})
    assert missing and all(c.blocks for c in missing)
    names = {f.key for f in target.fields if not f.optional}
    assert {c.label.split()[0] for c in missing} == names


def test_the_provider_password_is_optional_and_never_a_knob():
    """Most providers install the key at order time, so it is usually not
    needed -- and nothing on the server or in the inventory keeps it."""
    doc = registry.load()
    target = next(s for s in steps_mod.build(doc) if s.name == "target")
    field = next(f for f in target.fields if f.key == steps_mod.PROVIDER_PASSWORD)
    assert field.secret and field.optional
    assert not any(e["key"] == steps_mod.PROVIDER_PASSWORD
                   for e in [*doc["secrets"], *doc["config"]])


# --- the access page ---------------------------------------------------------

def test_the_panel_is_reached_through_the_panel_accounts_forward():
    way = steps_mod.way_in({"HOST_PUBLIC_IP": "203.0.113.10",
                            "SSH_PRIVATE_KEY": "~/.ssh/k"})
    assert way.command == (
        "ssh -N -L 9010:127.0.0.1:9010 -L 9000:127.0.0.1:9000 -i ~/.ssh/k "
        "panel@203.0.113.10")
    assert way.urls == ["http://localhost:9010", "http://localhost:9000"]


def test_a_non_default_port_rides_the_forward():
    way = steps_mod.way_in({"HOST_PUBLIC_IP": "203.0.113.10", "HOST_SSH_PORT": "2222"})
    assert " -p 2222 panel@203.0.113.10" in way.command


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
    doc = registry.load()
    assert registry.steps(doc)[-1]["name"] == "keyset"
