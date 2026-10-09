"""healthchecks-seed.py keeps an email channel to the admin while outgoing mail
is on: verified, notifying on down and on up, attached to every check when it
is created and never again, updated in place, and removed when mail is turned
off. A channel the client added is never touched.

The real seed script runs against the stand-in Django models of
healthchecks_seed_standins.py.

Run: uv run pytest tests/unit/test_healthchecks_seed_email_channel.py
"""
from __future__ import annotations

import json

from healthchecks_seed_standins import BASE_CONFIG, install_fake_django, run_seed

ADMIN = BASE_CONFIG["admin_email"]


def _seed(monkeypatch, *, mail: bool) -> None:
    run_seed(mail_enabled="true" if mail else "false")


def _email_channels(managers) -> list:
    return [c for c in managers["channel"].rows if getattr(c, "kind", "") == "email"]


def test_mail_on_seeds_a_verified_channel_to_the_admin(monkeypatch, capsys):
    managers = install_fake_django(monkeypatch)
    _seed(monkeypatch, mail=True)
    [channel] = _email_channels(managers)
    assert json.loads(channel.value) == {"value": ADMIN, "up": True, "down": True}
    assert channel.email_verified is True, (
        "Healthchecks sends nothing to an unverified email channel")
    assert f"email={channel.code}" in capsys.readouterr().out


def test_the_channel_reaches_checks_that_already_exist(monkeypatch):
    """Mail is configured after the install, so the channel arrives on a host
    whose checks exist: it attaches to every check in the project."""
    managers = install_fake_django(monkeypatch)
    _seed(monkeypatch, mail=False)
    project = managers["project"].rows[0]
    gatus = managers["check"].create(project=project, slug="gatus-auth")
    assert _email_channels(managers) == []

    _seed(monkeypatch, mail=True)
    [channel] = _email_channels(managers)
    assert all(channel in c.channel_set.added for c in managers["check"].rows)
    assert channel in gatus.channel_set.added


def test_an_update_keeps_the_bindings_and_never_reattaches(monkeypatch):
    """A stale address is rewritten in place, and a check the client detached
    the channel from stays detached."""
    managers = install_fake_django(monkeypatch)
    _seed(monkeypatch, mail=False)
    project = managers["project"].rows[0]
    for slug in ("catena-backup-attempted", "catena-backup-succeeded"):
        managers["check"].create(project=project, slug=slug)
    _seed(monkeypatch, mail=True)
    [channel] = _email_channels(managers)
    detached, kept = managers["check"].rows
    detached.channel_set.remove(channel)
    channel.value = json.dumps({"value": "old@example.com", "up": True, "down": True})

    _seed(monkeypatch, mail=True)
    assert _email_channels(managers) == [channel]
    assert json.loads(channel.value)["value"] == ADMIN
    assert channel not in detached.channel_set.added
    assert channel in kept.channel_set.added


def test_mail_turned_off_removes_the_channel(monkeypatch, capsys):
    managers = install_fake_django(monkeypatch)
    _seed(monkeypatch, mail=True)
    assert len(_email_channels(managers)) == 1
    _seed(monkeypatch, mail=False)
    assert _email_channels(managers) == []
    assert "email=none" in capsys.readouterr().out


def test_a_client_added_email_channel_is_never_touched(monkeypatch):
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username=ADMIN, email=ADMIN)
    project = managers["project"].create(owner=operator, name="vps-1")
    mine = managers["channel"].create(
        project=project, kind="email", value="me@example.com", email_verified=False)
    for mail in (True, False):
        _seed(monkeypatch, mail=mail)
    assert mine in managers["channel"].rows
    assert mine.value == "me@example.com" and mine.email_verified is False
