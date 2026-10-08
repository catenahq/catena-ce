"""healthchecks-seed.py seeds an ntfy channel only when both NTFY_SERVER and
NTFY_TOPIC are set, says so when it seeds none, attaches it to every check
when it creates it, never touches a channel the client added, and sets a
fresh profile's theme once (scripts/healthchecks-seed.py says why).

The real seed script runs here against a stand-in for the Django models it
touches, so the branches are exercised rather than pattern-matched.

Run: uv run pytest tests/unit/test_healthchecks_seed_ntfy_optional.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

SEED = (
    Path(__file__).resolve().parents[2] / "scripts" / "healthchecks-seed.py"
)

BASE_ENV = {
    "CATENA_ADMIN_EMAIL": "ops@example.com",
    "CATENA_HC_SUPERUSER_PASSWORD": "hunter2",
    "CATENA_INVENTORY_HOSTNAME": "vps-1",
    "CATENA_HC_API_KEY_READONLY": "ro-key",
    "CATENA_HC_API_KEY_READWRITE": "rw-key",
    "CATENA_HC_PING_KEY": "ping-key",
    "CATENA_MAIL_ENABLED": "false",
}


class _Row:
    """One stand-in model instance. Attribute bag with the few behaviours the
    seed script uses. `managers` is set per test, so a channel can attach
    itself to the project's checks and a check to the project's channels, as
    Healthchecks's Channel.assign_all_checks() and Check.assign_all_channels()
    do."""

    _next_code = 0
    managers: dict = {}

    def __init__(self, **fields):
        type(self)._next_code += 1
        self.code = f"code-{type(self)._next_code}"
        self.email = ""
        self.name = ""
        self.is_staff = True
        self.is_superuser = True
        self.channel_set = _ChannelSet()
        for key, value in fields.items():
            setattr(self, key, value)

    def save(self, **_kwargs):
        pass

    def assign_all_checks(self):
        for check in self.managers["check"].rows:
            if check.project is self.project:
                check.channel_set.add(self)

    def assign_all_channels(self):
        self.channel_set.added = [
            c for c in self.managers["channel"].rows if c.project is self.project]


class _ChannelSet:
    """A check's channel bindings (Healthchecks's Check.channel_set)."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        if obj not in self.added:
            self.added.append(obj)

    def remove(self, obj):
        self.added.remove(obj)


class _QuerySet:
    def __init__(self, manager, rows):
        self._manager = manager
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def exists(self):
        return bool(self._rows)

    def delete(self):
        for row in self._rows:
            self._manager.rows.remove(row)
        return len(self._rows), {}


class _Manager:
    def __init__(self):
        self.rows: list[_Row] = []

    def _match(self, **kwargs):
        return [
            r for r in self.rows
            if all(getattr(r, k, None) == v for k, v in kwargs.items())
        ]

    def filter(self, **kwargs):
        return _QuerySet(self, self._match(**kwargs))

    def create(self, **kwargs):
        row = _Row(**kwargs)
        self.rows.append(row)
        return row

    def create_superuser(self, **kwargs):
        return self.create(**kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        found = self._match(**kwargs)
        if found:
            for key, value in (defaults or {}).items():
                setattr(found[0], key, value)
            return found[0], False
        row = self.create(**{**kwargs, **(defaults or {})})
        return row, True


class _ProfileManager(_Manager):
    """Stand-in for Healthchecks's ProfileManager.

    for_user() get-or-creates, and a fresh profile's `theme` is None -- NOT "".
    That distinction is the whole point: None means "never chosen", "" means the
    client picked Light. A stub that returned "" here would let a regression
    through.
    """

    def for_user(self, user):
        found = self._match(user=user)
        if found:
            return found[0]
        row = self.create(user=user)
        row.theme = None
        return row


def _install_fake_django(monkeypatch) -> dict[str, _Manager]:
    """Register the module graph the seed imports, and return the managers so
    a test can inspect what the script did."""
    managers = {name: _Manager() for name in ("user", "project", "channel", "check")}
    managers["profile"] = _ProfileManager()
    monkeypatch.setattr(_Row, "managers", managers)

    def _model(manager):
        return type("Model", (), {"objects": manager})

    auth = types.ModuleType("django.contrib.auth")
    auth.get_user_model = lambda: _model(managers["user"])
    accounts_models = types.ModuleType("hc.accounts.models")
    accounts_models.Project = _model(managers["project"])
    accounts_models.Profile = _model(managers["profile"])
    api_models = types.ModuleType("hc.api.models")
    api_models.Channel = _model(managers["channel"])
    api_models.Check = _model(managers["check"])

    for name, mod in {
        "django": types.ModuleType("django"),
        "django.contrib": types.ModuleType("django.contrib"),
        "django.contrib.auth": auth,
        "hc": types.ModuleType("hc"),
        "hc.accounts": types.ModuleType("hc.accounts"),
        "hc.accounts.models": accounts_models,
        "hc.api": types.ModuleType("hc.api"),
        "hc.api.models": api_models,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    return managers


def _run_seed(monkeypatch, capsys, *, server: str, topic: str):
    managers = _install_fake_django(monkeypatch)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATENA_NTFY_SERVER", server)
    monkeypatch.setenv("CATENA_NTFY_TOPIC", topic)
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    return managers, capsys.readouterr().out


def test_a_configured_ntfy_seeds_a_channel(monkeypatch, capsys):
    managers, out = _run_seed(
        monkeypatch, capsys, server="https://ntfy.internal", topic="s3cret",
    )
    channels = managers["channel"].rows
    assert len(channels) == 1
    assert "s3cret" in channels[0].value
    assert "https://ntfy.internal" in channels[0].value
    assert "channel=none" not in out


def test_a_new_ntfy_channel_reaches_checks_that_already_exist(monkeypatch, capsys):
    """Set after the checks exist, the channel attaches to every check in the
    project: a gatus-* check made before the Alerts save pages through it
    too."""
    managers = _install_fake_django(monkeypatch)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATENA_NTFY_SERVER", "")
    monkeypatch.setenv("CATENA_NTFY_TOPIC", "")
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    project = managers["project"].rows[0]
    gatus = managers["check"].create(project=project, slug="gatus-auth")

    monkeypatch.setenv("CATENA_NTFY_SERVER", "https://ntfy.internal")
    monkeypatch.setenv("CATENA_NTFY_TOPIC", "s3cret")
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    ntfy = managers["channel"].rows[0]
    assert all(ntfy in c.channel_set.added for c in managers["check"].rows)
    assert ntfy in gatus.channel_set.added


def test_a_detached_ntfy_channel_stays_detached(monkeypatch, capsys):
    """A client who removes Catena's channel from a check keeps that choice:
    the channel is attached when it is created and never again."""
    managers, _ = _run_seed(monkeypatch, capsys, server="", topic="")
    check = managers["check"].create(
        project=managers["project"].rows[0], slug="catena-backup-attempted")
    monkeypatch.setenv("CATENA_NTFY_SERVER", "https://ntfy.internal")
    monkeypatch.setenv("CATENA_NTFY_TOPIC", "s3cret")
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    ntfy = managers["channel"].rows[0]
    check.channel_set.remove(ntfy)
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    assert ntfy not in check.channel_set.added


@pytest.mark.parametrize(
    ("server", "topic"),
    [
        ("", ""),
        ("https://ntfy.internal", ""),   # server without a topic delivers nowhere
        ("", "s3cret"),                  # topic without a server has no target
    ],
)
def test_an_incomplete_ntfy_config_seeds_no_channel(
    monkeypatch, capsys, server, topic,
):
    managers, out = _run_seed(monkeypatch, capsys, server=server, topic=topic)
    assert managers["channel"].rows == [], (
        "a half-configured ntfy must not produce a channel: it reads as "
        "configured in the UI and delivers nothing"
    )
    assert "no ntfy channel configured" in out
    assert "channel=none" in out


def test_the_seed_creates_no_check(monkeypatch, capsys):
    """The scheduled lanes' checks belong to catena-admin's catena-schedule,
    which keeps them on the lanes' schedule; a check the seed made would be a
    second owner holding another one."""
    managers, _ = _run_seed(
        monkeypatch, capsys, server="https://ntfy.internal", topic="s3cret",
    )
    assert managers["check"].rows == []


def test_clearing_the_config_removes_a_previously_seeded_channel(
    monkeypatch, capsys,
):
    """Reconcilable in both directions: an operator who removes the values
    must not be left with a stale channel nobody can tell is dead."""
    managers = _install_fake_django(monkeypatch)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setenv("CATENA_NTFY_SERVER", "https://ntfy.internal")
    monkeypatch.setenv("CATENA_NTFY_TOPIC", "s3cret")
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    assert len(managers["channel"].rows) == 1

    monkeypatch.setenv("CATENA_NTFY_SERVER", "")
    monkeypatch.setenv("CATENA_NTFY_TOPIC", "")
    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    assert managers["channel"].rows == []
    assert "Removed 1 stale ntfy channel(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("server", "topic"), [("https://ntfy.internal", "s3cret"), ("", "")],
)
def test_a_converge_leaves_client_added_channels_alone(
    monkeypatch, capsys, server, topic,
):
    """Only the channel the seed created is updated or removed: a client's
    email channel and their own ntfy channel survive either way."""
    managers = _install_fake_django(monkeypatch)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATENA_NTFY_SERVER", server)
    monkeypatch.setenv("CATENA_NTFY_TOPIC", topic)
    operator = managers["user"].create(username=BASE_ENV["CATENA_ADMIN_EMAIL"],
                                       email=BASE_ENV["CATENA_ADMIN_EMAIL"])
    project = managers["project"].create(owner=operator, name="vps-1")
    email = managers["channel"].create(project=project, kind="email",
                                       value="me@example.com")
    own_ntfy = managers["channel"].create(project=project, kind="ntfy",
                                          value='{"topic": "mine"}')

    for _ in range(2):
        exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})

    rows = managers["channel"].rows
    assert email in rows and email.value == "me@example.com"
    assert own_ntfy in rows and own_ntfy.value == '{"topic": "mine"}'
    seeded = [r for r in rows if r not in (email, own_ntfy)]
    assert len(seeded) == (1 if server else 0)


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
    # The value has to be one the view accepts, or it is stored and ignored.
    assert '"system"' in body


def test_the_theme_write_touches_only_the_theme():
    """A bare .save() would rewrite every column on the profile, including any
    the client changed through the UI between converges."""
    body = SEED.read_text()
    assert '_profile.save(update_fields=["theme"])' in body


def test_a_fresh_profile_follows_the_browser_preference(monkeypatch, capsys):
    """A never-touched profile ends up on the "system" theme."""
    managers, _ = _run_seed(monkeypatch, capsys, server="", topic="")
    profiles = managers["profile"].rows
    assert len(profiles) == 1, f"expected one profile, got {profiles}"
    assert profiles[0].theme == "system"


def test_a_client_who_chose_light_keeps_it(monkeypatch, capsys):
    """A profile set to Light ("") keeps it across a converge."""
    managers = _install_fake_django(monkeypatch)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATENA_NTFY_SERVER", "")
    monkeypatch.setenv("CATENA_NTFY_TOPIC", "")

    # Pre-seed the operator and a profile that has been set to Light.
    operator = managers["user"].create(username=BASE_ENV["CATENA_ADMIN_EMAIL"],
                                       email=BASE_ENV["CATENA_ADMIN_EMAIL"])
    chosen = managers["profile"].create(user=operator)
    chosen.theme = ""

    exec(compile(SEED.read_text(), str(SEED), "exec"), {"__name__": "__seed__"})
    assert chosen.theme == "", "a deliberate Light choice was overwritten"
