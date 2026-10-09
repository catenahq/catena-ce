"""healthchecks-seed.py keeps one superuser and one project across an admin
email change, creates no check, leaves every channel it does not own alone,
and sets a fresh profile's theme once (scripts/healthchecks-seed.py says why).

The real seed script runs against the stand-in Django models of
healthchecks_seed_standins.py.

Run: uv run pytest tests/unit/test_healthchecks_seed.py
"""
from __future__ import annotations

import json

from healthchecks_seed_standins import BASE_ENV, SEED, install_fake_django, run_seed

ADMIN = BASE_ENV["CATENA_ADMIN_EMAIL"]


def test_an_admin_email_change_renames_the_one_superuser(monkeypatch):
    """Found by its new email the superuser would be a second one, whose
    project can never take the pinned ping key (unique in Healthchecks)."""
    managers = install_fake_django(monkeypatch)
    run_seed(monkeypatch, CATENA_MAIL_ENABLED="true")
    [operator] = managers["user"].rows
    [project] = managers["project"].rows
    [channel] = managers["channel"].rows
    check = managers["check"].create(project=project, slug="gatus-auth")
    check.channel_set.add(channel)

    run_seed(monkeypatch, CATENA_MAIL_ENABLED="true", CATENA_ADMIN_EMAIL="new@example.com")
    assert managers["user"].rows == [operator]
    assert operator.username == operator.email == "new@example.com"
    assert operator.is_superuser and operator.is_staff
    assert managers["project"].rows == [project]
    assert project.ping_key == BASE_ENV["CATENA_HC_PING_KEY"]
    assert managers["channel"].rows == [channel]
    assert json.loads(channel.value)["value"] == "new@example.com"
    assert channel in check.channel_set.added


def test_the_seed_creates_no_check(monkeypatch):
    """The scheduled lanes' checks belong to catena-admin's catena-schedule,
    which keeps them on the lanes' schedule; a check the seed made would be a
    second owner holding another one."""
    managers = install_fake_django(monkeypatch)
    run_seed(monkeypatch, CATENA_MAIL_ENABLED="true")
    assert managers["check"].rows == []


def test_a_channel_the_seed_does_not_own_is_left_alone(monkeypatch):
    """A client's channels, and a push channel an earlier converge created
    under its own code, are neither updated nor removed, with mail on or off."""
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username=ADMIN, email=ADMIN)
    project = managers["project"].create(owner=operator, name="vps-1")
    mine = managers["channel"].create(project=project, kind="email",
                                      value="me@example.com")
    ntfy = managers["channel"].create(project=project, kind="ntfy",
                                      value='{"topic": "s3cret"}', name="ntfy (vps-1)")
    for mail in ("true", "false", "true"):
        run_seed(monkeypatch, CATENA_MAIL_ENABLED=mail)
    assert mine in managers["channel"].rows and mine.value == "me@example.com"
    assert ntfy in managers["channel"].rows and ntfy.value == '{"topic": "s3cret"}'


def test_the_seed_reads_no_push_channel_setting():
    assert "NTFY" not in SEED.read_text()


# --- theme default ---

def test_the_theme_default_is_set_once_and_never_argued_with():
    """The seed sets "system" only on a profile whose theme is still NULL, and
    writes a value the accounts view accepts."""
    body = SEED.read_text()
    assert 'Profile.objects.for_user(operator)' in body, (
        "the profile is not resolved through the manager, which get-or-creates")
    assert '_profile.theme is None' in body, (
        "the theme is set unconditionally: a client who chose Light would have "
        "it overwritten on every converge")
    assert '_profile.theme = "system"' in body


def test_the_theme_write_touches_only_the_theme():
    """A bare .save() would rewrite every column on the profile, including any
    the client changed through the UI between converges."""
    assert '_profile.save(update_fields=["theme"])' in SEED.read_text()


def test_a_fresh_profile_follows_the_browser_preference(monkeypatch):
    managers = install_fake_django(monkeypatch)
    run_seed(monkeypatch)
    [profile] = managers["profile"].rows
    assert profile.theme == "system"


def test_a_client_who_chose_light_keeps_it(monkeypatch):
    managers = install_fake_django(monkeypatch)
    operator = managers["user"].create(username=ADMIN, email=ADMIN)
    chosen = managers["profile"].create(user=operator)
    chosen.theme = ""
    run_seed(monkeypatch)
    assert chosen.theme == "", "a deliberate Light choice was overwritten"
