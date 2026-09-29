"""helpers/install_key.py --check-password: does the provider's password open the
initial login, asked without changing anything.

The graphical installer asks this before an install, for a server that does not
accept the key yet. A password the server wants changed at first login OPENS
it -- the install answers that prompt itself -- and the check must not answer
it, or the password the client typed stops being the one the install uses.

Run: uv run pytest tests/unit/test_install_key_checks_a_password.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pexpect
import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import install_key as ik  # noqa: E402

_PROMPTS = len(ik._CHANGE_WARNINGS + ik._CURRENT_PW_PROMPTS + ik._NEW_PASSWORD_PROMPTS)
_REFUSED = _PROMPTS
_CLOSED = _PROMPTS + len(ik._AUTH_FAILED)
_SHELL = _CLOSED + 1


class _Child:
    """A scripted ssh session: each expect() returns the next index."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent: list[str] = []

    def expect(self, patterns, timeout=None):
        return self.answers.pop(0)

    def expect_exact(self, text, timeout=None):
        return 0

    def sendline(self, text):
        self.sent.append(text)

    def close(self, force=False):
        pass


@pytest.fixture
def session(monkeypatch):
    def script(*answers):
        child = _Child(answers)
        monkeypatch.setattr(ik, "_spawn_password_ssh", lambda h, u, p: child)
        return child
    return script


def test_a_password_that_reaches_a_shell_opens_the_login(session):
    child = session(0, _SHELL)
    opened, _ = ik._password_opens("203.0.113.10", "debian", "pw")
    assert opened
    assert child.sent[0] == "pw"


def test_a_refused_password_does_not(session):
    session(0, _REFUSED)
    opened, why = ik._password_opens("203.0.113.10", "debian", "pw")
    assert not opened and "refused" in why


def test_a_forced_change_opens_it_and_is_left_unanswered(session):
    child = session(0, 0)
    opened, why = ik._password_opens("203.0.113.10", "debian", "pw")
    assert opened and "new one" in why
    assert child.sent == ["pw"], "the check answered the change prompt"


def test_a_server_that_takes_no_password_says_so(session):
    child = session(1)
    opened, why = ik._password_opens("203.0.113.10", "root", "pw")
    assert not opened and "takes no password" in why
    assert child.sent == []


def test_a_session_that_stalls_is_not_an_opening(session, monkeypatch):
    child = session(0, _SHELL)

    def stall(text, timeout=None):
        raise pexpect.TIMEOUT("no marker")
    monkeypatch.setattr(child, "expect_exact", stall)
    opened, _ = ik._password_opens("203.0.113.10", "debian", "pw")
    assert not opened
