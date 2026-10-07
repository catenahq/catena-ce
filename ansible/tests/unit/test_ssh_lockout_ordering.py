"""The sshd handoff proves the new path before it closes the old ones.

bootstrap/roles/common performs the second of the two irreversible access
handoffs in the product, and both carry a gate:

    ufw_lockdown.yml:        add tailnet rule -> VERIFY -> remove public 22
    bootstrap/roles/common:  install ops key  -> VERIFY -> deny everything else

The install runs on the server in two legs. The first creates ops with the
installer's key through the provider's login and changes nothing else. The
installer then logs in as ops with that key, and the second leg proves that
session from sshd's own record before its first role hardens sshd. The order
these tests pin:

    provider login: ops + key installed, nothing closed
      -> reconnect as ops with the key; the server proves the session
      -> deny the provider account, lock sshd

Ordering is the property, so the tests assert on ORDER, not on presence: a
proof that runs after the hardening proves nothing, and would pass every
substring check.

Run: uv run pytest tests/unit/test_ssh_lockout_ordering.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ansible_tree import task_index as _index

ANSIBLE = Path(__file__).resolve().parents[2]
COMMON = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks"
ACCOUNTS_PLAYBOOK = ANSIBLE / "playbooks" / "accounts.yml"
INSTALL_HOST = ANSIBLE / "install-host.sh"


def _tasks(path: Path) -> list[dict]:
    return [t for t in (yaml.safe_load(path.read_text()) or []) if isinstance(t, dict)]


def test_the_key_is_installed_then_proved_then_sshd_is_locked():
    """The whole defect in one assertion: hardening on the strength of a
    non-empty variable."""
    tasks = _tasks(COMMON / "main.yml")
    accounts = next(i for i, t in enumerate(tasks)
                    if (t.get("ansible.builtin.import_tasks") or "") == "accounts.yml")
    gate = _index(tasks, "refuse to harden")
    harden = _index(tasks, "Drop sshd hardening config")
    assert accounts < gate < harden


def test_the_gate_stops_the_play_unless_the_session_was_proved():
    tasks = _tasks(COMMON / "main.yml")
    gate = tasks[_index(tasks, "refuse to harden")]
    assert "ansible.builtin.fail" in gate, "the gate does not stop the play"
    conditions = " ".join(str(c) for c in gate.get("when", []))
    assert "catena_install_key_proven | default(false)" in conditions
    assert "skip_ssh_lockout_gate | default(false)" in conditions, (
        "the bypass must exist and default off")


def test_the_server_proves_the_session_before_the_hardening_leg_runs():
    """install-host.sh refuses the system leg unless session_proof.py matches
    sshd's record of this session to a key ops holds, and only that leg sets
    the variable the gate reads."""
    script = INSTALL_HOST.read_text()
    proof = script.index('python3 "$ansible_dir/helpers/session_proof.py"')
    first_play = script.index("play bootstrap.yml")
    assert proof < first_play
    assert 'doc["catena_install_key_proven"] = True' in script
    assert 'if sys.argv[3] == "system":' in script


def test_the_first_leg_creates_the_accounts_and_closes_nothing():
    """It runs through the provider's login, which must keep working until
    ops has proved itself."""
    play = yaml.safe_load(ACCOUNTS_PLAYBOOK.read_text())[0]
    included = [t["ansible.builtin.include_role"] for t in play["tasks"]]
    assert included == [{"name": "common", "tasks_from": "accounts"}]
    text = (COMMON / "accounts.yml").read_text()
    for closing in ("sshd", "ufw", "AllowUsers", "PasswordAuthentication"):
        assert closing not in text, closing
