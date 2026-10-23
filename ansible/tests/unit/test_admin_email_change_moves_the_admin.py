"""An admin email change moves the admin's Keycloak accounts to the new email
and adds none: one realm admin, the same account, holding the new email and
keeping its password and second factor (CV2).

The realm seed keys the realm admin on admin_email (keycloak-config-cli creates
a user it does not find by username and deletes none), and the master realm's
second factor is asked of the master account named by it, so the converge moves
the accounts by id before the import, on the automation client's session: the
master realm's, whose username Keycloak changes only while the realm allows
username edits, and this realm's, whose username follows its email. The task's
script runs here against a stand-in `docker` that answers the kcadm calls the
way Keycloak 26.6.7's admin API does (DeclarativeUserProfileProviderFactory
editUsernameCondition, DefaultAttributes email-as-username).

Run: uv run pytest tests/unit/test_admin_email_change_moves_the_admin.py
"""
from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "keycloak"
BOOTSTRAP = ROLE / "tasks" / "realm_bootstrap.yml"
REALM = ROLE / "templates" / "realm-vps.yaml.j2"

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

OLD, NEW = "old@acme.test", "new@acme.test"
CT = "keycloak_server.1.abc"

# Answers the kcadm calls the task makes, from a JSON state file.
FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    state_path = os.environ["FAKE_KC_STATE"]
    state = json.load(open(state_path))
    state["argv"].append(sys.argv[1:])
    args = sys.argv[1:]
    def save():
        json.dump(state, open(state_path, "w"))
    assert args[:3] == ["exec", "keycloak_server.1.abc", "/opt/keycloak/bin/kcadm.sh"], args
    kc = args[3:]
    def opt(name):
        return [kc[i + 1] for i, a in enumerate(kc) if a == name]
    if kc[:2] == ["get", "users"]:
        realm = state["realms"][opt("-r")[0]]
        want = [q.split("=", 1)[1] for q in opt("-q") if q.startswith("username=")][0]
        for u in realm["users"]:
            if u["username"] == want:
                print(u["id"])
        save(); sys.exit(0)
    if kc[:2] == ["get", "realms/master"]:
        print(str(state["realms"]["master"]["editUsernameAllowed"]).lower())
        save(); sys.exit(0)
    if kc[0] == "update" and kc[1] == "realms/master":
        state["realms"]["master"]["editUsernameAllowed"] = opt("-s")[0].split("=")[1] == "true"
        save(); sys.exit(0)
    if kc[0] == "update" and kc[1].startswith("users/"):
        realm = state["realms"][opt("-r")[0]]
        user = next(u for u in realm["users"] if u["id"] == kc[1].split("/")[1])
        for s in opt("-s"):
            key, value = s.split("=", 1)
            if key == "email":
                user["email"] = value
                if realm["emailAsUsername"]:
                    user["username"] = value
            elif key == "username" and realm["editUsernameAllowed"] and not realm["emailAsUsername"]:
                user["username"] = value
        save(); sys.exit(0)
    sys.exit(f"unexpected kcadm call {kc}")
''')


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(tasks, fragment):
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _move_task() -> dict:
    tasks = _tasks()
    return tasks[_index(tasks, "move the admin's accounts")]


def _script(recorded: str, current: str) -> str:
    task = _move_task()
    variables = {
        "_kc_recorded_admin_email": recorded, "admin_email": current,
        "keycloak_realm": "vps", "_kc_master_login": {"stdout": CT},
    }
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.shell"]))


def _run(tmp_path: Path, realms: dict, recorded=OLD, current=NEW):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"realms": realms, "argv": []}))
    proc = subprocess.run(
        ["bash", "-c", _script(recorded, current)], text=True, capture_output=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    return proc, json.loads(state.read_text())


def _realms(master_user=OLD, vps_users=None, master_users=None):
    return {
        "master": {"emailAsUsername": False, "editUsernameAllowed": False,
                   "users": master_users if master_users is not None
                   else [{"id": "m1", "username": master_user, "email": ""}]},
        "vps": {"emailAsUsername": True, "editUsernameAllowed": False,
                "users": vps_users if vps_users is not None
                else [{"id": "v1", "username": OLD, "email": OLD}]},
    }


def test_both_accounts_take_the_new_email_and_keep_their_ids(tmp_path):
    proc, state = _run(tmp_path, _realms())
    assert proc.returncode == 0, proc.stderr
    master, vps = state["realms"]["master"], state["realms"]["vps"]
    assert master["users"] == [{"id": "m1", "username": NEW, "email": ""}]
    assert master["editUsernameAllowed"] is False, "username edits were left allowed"
    assert vps["users"] == [{"id": "v1", "username": NEW, "email": NEW}]
    assert "moved: master" in proc.stdout and "moved: vps" in proc.stdout


def test_the_move_reads_no_credential_and_runs_on_the_shared_session():
    """The automation client's session, opened by the sign-in before it."""
    task = _move_task()
    script = task["ansible.builtin.shell"]
    assert "stdin" not in (task.get("args") or {})
    assert "admin_password" not in script and "config credentials" not in script
    assert "{{ _kc_master_login.stdout | quote }}" in script
    tasks = _tasks()
    assert _index(tasks, "sign kcadm in to the master realm") < _index(tasks, "move the admin's accounts")


def test_a_master_admin_already_moved_is_left_alone(tmp_path):
    """A rerun after a failed import: the master account holds the new email."""
    proc, state = _run(tmp_path, _realms(master_user=NEW))
    assert proc.returncode == 0, proc.stderr
    assert "moved: master" not in proc.stdout
    assert state["realms"]["vps"]["users"][0]["username"] == NEW


@pytest.mark.parametrize("realm", ["master", "vps"])
def test_an_email_already_held_in_a_realm_moves_no_one_there(tmp_path, realm):
    if realm == "master":
        users = [{"id": "m1", "username": OLD, "email": ""},
                 {"id": "m2", "username": NEW, "email": ""}]
        realms = _realms(master_users=[dict(u) for u in users])
    else:
        users = [{"id": "v1", "username": OLD, "email": OLD},
                 {"id": "v2", "username": NEW, "email": NEW}]
        realms = _realms(vps_users=[dict(u) for u in users])
    proc, state = _run(tmp_path, realms)
    assert proc.returncode == 0, proc.stderr
    assert state["realms"][realm]["users"] == users
    assert f"hold an account in {realm}: neither was moved" in proc.stderr


def test_it_runs_before_both_imports_and_only_on_a_change():
    tasks = _tasks()
    read = _index(tasks, "the admin email the last import ran under")
    keep = _index(tasks, "keep the admin email the last import ran under")
    move = _index(tasks, "move the admin's accounts")
    seed = _index(tasks, "seed the admin password and the realm settings")
    imp = _index(tasks, "import the realm config")
    record = _index(tasks, "record the admin email this import ran under")
    assert read < keep < move < seed < imp < record
    assert _move_task()["when"] == [
        "_kc_recorded_admin_email | length > 0",
        "_kc_recorded_admin_email != admin_email",
    ]
    kept = tasks[keep]["ansible.builtin.set_fact"]["_kc_recorded_admin_email"]
    assert "_kc_admin_email_record.content" in kept and "b64decode" in kept
    assert tasks[record]["ansible.builtin.copy"]["content"] == "{{ admin_email }}\n"
    assert tasks[read]["ansible.builtin.slurp"]["src"] == tasks[record]["ansible.builtin.copy"]["dest"]


def test_the_realm_file_keys_the_admin_on_admin_email():
    """The premise: the seed import matches the admin by this username."""
    assert '- username: "{{ admin_email }}"' in REALM.read_text()
