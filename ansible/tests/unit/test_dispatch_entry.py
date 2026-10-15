"""The forced command refuses a request carrying more than an action name
before sudo runs (scripts/catena-dispatch-entry.sh).

sudo logs the command line it runs and the process table shows it, so the
entry is the last place a request's payload can be turned away unseen. Run
for real under bash, from a copy whose sudo is a stub that records what it
receives: everything before the exec is the shipped script.

Run: uv run pytest tests/unit/test_dispatch_entry.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
ENTRY = ANSIBLE / "scripts" / "catena-dispatch-entry.sh"
SUDO = "/usr/bin/sudo"


@pytest.fixture
def entry(tmp_path):
    """Return a callable: entry(request) -> (CompletedProcess, sudo argv or None)."""
    generation = tmp_path / "0123456789ab"
    generation.mkdir()
    record = tmp_path / "sudo-argv"
    stub = tmp_path / "sudo"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{record}"\n')
    stub.chmod(0o755)
    text = ENTRY.read_text(encoding="utf-8")
    assert text.count(SUDO) == 1, "the entry execs sudo by another path"
    copy = generation / "entry"
    copy.write_text(text.replace(SUDO, str(stub)), encoding="utf-8")
    copy.chmod(0o755)

    def run(request: str | None):
        env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
        if request is not None:
            env["SSH_ORIGINAL_COMMAND"] = request
        r = subprocess.run(["bash", str(copy)], capture_output=True, text=True,
                           env=env, timeout=30)
        argv = record.read_text().splitlines() if record.exists() else None
        return r, argv

    run.dispatcher = str(generation.resolve() / "dispatcher")  # type: ignore[attr-defined]
    return run


def test_an_action_name_reaches_sudo_as_its_one_argument(entry):
    r, argv = entry("backup-now")
    assert r.returncode == 0, r.stderr
    assert argv == ["-n", entry.dispatcher, "backup-now"]


@pytest.mark.parametrize("request_", [
    "panel-echo c2VjcmV0LWtleQ==",
    "panel-echo\tc2VjcmV0LWtleQ==",
    "panel-echo\nc2VjcmV0LWtleQ==",
    "panel-echo;c2VjcmV0LWtleQ==",
], ids=["space", "tab", "newline", "punctuation"])
def test_a_request_carrying_more_than_the_name_never_reaches_sudo(entry, request_):
    r, argv = entry(request_)
    assert r.returncode == 2
    assert argv is None, "sudo ran, and logged the request"
    assert "c2VjcmV0LWtleQ==" not in r.stderr, "the refusal echoed the payload"
    assert "CATENA_DISPATCH_PAYLOAD" in r.stderr


def test_the_refusal_names_a_first_word_only_when_it_is_an_action_name(entry):
    named, _ = entry("panel-echo c2VjcmV0")
    assert "refusing 'panel-echo'" in named.stderr
    unnamed, _ = entry("x=c2VjcmV0 y")
    assert "c2VjcmV0" not in unnamed.stderr
    assert "not an action name" in unnamed.stderr


@pytest.mark.parametrize("request_", [None, ""], ids=["unset", "empty"])
def test_an_empty_request_never_reaches_sudo(entry, request_):
    r, argv = entry(request_)
    assert r.returncode == 2
    assert argv is None


def test_the_entry_hands_the_name_to_its_own_generations_dispatcher():
    """The sudo rule a generation installs names its own dispatcher, so the
    entry has to run that one and no other."""
    text = ENTRY.read_text(encoding="utf-8")
    assert 'exec /usr/bin/sudo -n "$here/dispatcher" "$request"' in text
    assert 'here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"' in text
