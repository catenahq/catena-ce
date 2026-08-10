"""No ntfy configuration means no channel -- and a converge that says so.

Neither NTFY_SERVER nor NTFY_TOPIC defaults to a value: a default of
https://ntfy.sh would be public and unauthenticated, where the topic is the
only access control, so a host nobody configured would push its alerts to a
server the operator does not run. Worse, with only the server set the seed
would still create a channel carrying an empty topic -- a route that
delivers nowhere and reads in the UI as configured.

Both blank is a supported end state: checks still record every ping, the
client attaches their own channel through the Healthchecks integrations UI,
and the converge prints a notice so the silence is deliberate rather than
undiscovered.

The real seed script runs here against a stand-in for the two Django models it
touches, so the branch is exercised rather than pattern-matched.

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
}


class _Row:
    """One stand-in model instance. Attribute bag with the few behaviours the
    seed script uses."""

    _next_code = 0

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


class _ChannelSet:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


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


def _install_fake_django(monkeypatch) -> dict[str, _Manager]:
    """Register the module graph the seed imports, and return the managers so
    a test can inspect what the script did."""
    managers = {name: _Manager() for name in ("user", "project", "channel", "check")}

    def _model(manager):
        return type("Model", (), {"objects": manager})

    auth = types.ModuleType("django.contrib.auth")
    auth.get_user_model = lambda: _model(managers["user"])
    accounts_models = types.ModuleType("hc.accounts.models")
    accounts_models.Project = _model(managers["project"])
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


def test_a_configured_ntfy_still_seeds_and_binds_a_channel(monkeypatch, capsys):
    managers, out = _run_seed(
        monkeypatch, capsys, server="https://ntfy.internal", topic="s3cret",
    )
    channels = managers["channel"].rows
    assert len(channels) == 1
    assert "s3cret" in channels[0].value
    assert "https://ntfy.internal" in channels[0].value
    # Both backup lanes route to it.
    bound = [
        c for c in managers["check"].rows if channels[0] in c.channel_set.added
    ]
    assert len(bound) == 2, "both backup checks must bind the channel"
    assert "channel=none" not in out


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
    assert "no notification channel configured" in out
    assert "channel=none" in out


def test_the_checks_are_still_seeded_without_a_channel(monkeypatch, capsys):
    """No delivery is not no monitoring. The dead-man checks still exist, so
    the state is recorded and a channel added later starts working at once."""
    managers, _ = _run_seed(monkeypatch, capsys, server="", topic="")
    slugs = {getattr(c, "slug", "") for c in managers["check"].rows}
    assert "catena-backup-succeeded" in slugs
    assert "catena-backup-attempted" in slugs


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
