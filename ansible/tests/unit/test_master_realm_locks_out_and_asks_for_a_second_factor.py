"""The master realm's sign-in locks out repeated wrong passwords as the vps
realm's does, and its admin must set up and use a second factor (CV2).

Keycloak creates every realm with brute-force protection off and the numbers
the vps realm's seed repeats: a 60-second pause growing by 60 seconds per
failure, capped at 900 (RealmManager setupRealmDefaults, Keycloak 26.6.7). So
the converge switches the protection on in the master realm and keeps numbers
tuned there, and asks the master admin to set up a one-time code
(CONFIGURE_TOTP) while they hold none; once they hold one, the master realm's
browser flow asks for it at every sign-in. While Settings requires a second
factor, every other master-realm user holding the admin role is asked the same,
the automation service account excepted. The step's script runs here against
a stand-in `docker` that answers the kcadm calls from a JSON state file.

Run: uv run pytest tests/unit/test_master_realm_locks_out_and_asks_for_a_second_factor.py
"""
from __future__ import annotations

import json
import stat
from datetime import datetime
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from test_realm_bootstrap_settles import BOOTSTRAP, _render

STEP = "the master realm locks out wrong passwords and asks its admin for a second factor"
CT = "keycloak_server.1.abc"
ADMIN = "admin@acme.test"

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ["FAKE_KC_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    assert args[:3] == ["exec", "keycloak_server.1.abc", "/opt/keycloak/bin/kcadm.sh"], args
    kc = args[3:]
    state["calls"].append(kc)
    def opt(name):
        return kc[kc.index(name) + 1] if name in kc else None
    def done(code=0):
        json.dump(state, open(path, "w"))
        sys.exit(code)
    realm = state["master"]
    if kc[:2] == ["get", "realms/master"]:
        assert opt("--fields") == "bruteForceProtected" and "--noquotes" in kc, kc
        print(str(realm["bruteForceProtected"]).lower())
        done()
    if kc[:2] == ["update", "realms/master"]:
        key, value = opt("-s").split("=", 1)
        realm[key] = value == "true"
        done()
    assert opt("-r") == "master", kc
    if kc[0] == "get" and kc[1].endswith("/role-mappings/realm/composite"):
        assert opt("--fields") == "name" and "--noquotes" in kc, kc
        name, who = next((n, u) for n, u in realm["users"].items() if kc[1].startswith("users/u-" + n + "/"))
        if state.get("fail", {}).get("composite") == name:
            done(1)
        print("default-roles-master\\noffline_access")
        if who.get("admin") or who.get("via"):
            print("admin")
        done()
    if kc[0] == "get" and kc[1].endswith("/role-mappings/realm"):
        # The direct mappings: admin only for a direct holder. A composite holder
        # has the composite role "ops-lead" and a group holder has nothing here.
        who = next(u for n, u in realm["users"].items() if kc[1].startswith("users/u-" + n + "/"))
        print("admin" if who.get("admin") else "ops-lead" if who.get("via") == "composite" else "")
        done()
    if kc[:2] == ["get", "users"] and not any(a.startswith("username=") for a in kc):
        assert opt("--fields") == "id,username" and "--noquotes" in kc, kc
        for n in realm["users"]:
            print("u-" + n + "," + n)
        done()
    if kc[:2] == ["get", "users"]:
        queries = [kc[i + 1] for i, a in enumerate(kc) if a == "-q"]
        assert "exact=true" in queries, kc
        name = next(q.split("=", 1)[1] for q in queries if q.startswith("username="))
        if name in realm["users"]:
            print("u-" + name)
        done()
    user = next((u for n, u in realm["users"].items() if kc[1].startswith("users/u-" + n)), None)
    if kc[0] == "get" and kc[1].endswith("/credentials"):
        assert opt("--fields") == "type", kc
        print("\\n".join(user["credentials"]))
        done()
    if kc[0] == "get":
        assert opt("--fields") == "requiredActions", kc
        print(",".join(user["requiredActions"]))
        done()
    if kc[0] == "update":
        assert opt("-s") == "requiredActions+=CONFIGURE_TOTP", kc
        if state.get("fail", {}).get("update") and kc[1] == "users/u-" + state["fail"]["update"]:
            done(1)
        user["requiredActions"].append("CONFIGURE_TOTP")
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _task() -> dict:
    return next(t for t in yaml.safe_load(BOOTSTRAP.read_text())
                if STEP in str(t.get("name", "")))


def _master(protected=False, credentials=("password",), actions=(), users=(ADMIN,), **extra):
    return {"bruteForceProtected": protected, "failureFactor": 30, **extra,
            "users": {u: {"credentials": list(credentials), "requiredActions": list(actions),
                          "admin": True}
                      for u in users}}


def _add(master: dict, name: str, credentials=("password",), actions=(), admin=True, via=""):
    """via: "group" or "composite" gives the admin role without a direct mapping."""
    master["users"][name] = {"credentials": list(credentials),
                             "requiredActions": list(actions), "admin": admin and not via,
                             "via": via}
    return master


def _run(tmp_path: Path, master: dict, enforce: str = "false", fail: dict | None = None,
         succeeds: bool = True):
    task = _task()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"master": master, "calls": [], "fail": fail or {}}))
    variables = {"_keycloak_realm_server_ct": {"stdout": CT}, "admin_email": ADMIN,
                 "keycloak_enforce_mfa": enforce,
                 "keycloak_master_admins_path": str(tmp_path / "admins.json")}
    script = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.shell"]))
    proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    assert (proc.returncode == 0) == succeeds, proc.stderr
    variables[task["register"]] = {"stdout": proc.stdout}
    changed = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template("{{ " + task["changed_when"] + " }}"))
    return proc, changed, json.loads(state.read_text())


def test_a_fresh_master_realm_locks_out_and_asks_for_a_one_time_code(tmp_path):
    proc, changed, state = _run(tmp_path, _master())
    master = state["master"]
    assert master["bruteForceProtected"] is True
    assert master["users"][ADMIN]["requiredActions"] == ["CONFIGURE_TOTP"]
    assert "locks out: master" in proc.stdout
    assert f"asks for a second factor: {ADMIN}" in proc.stdout and changed


def test_an_admin_holding_a_one_time_code_is_asked_for_nothing(tmp_path):
    proc, changed, state = _run(tmp_path, _master(protected=True,
                                                   credentials=("password", "otp")))
    assert state["master"]["users"][ADMIN]["requiredActions"] == []
    assert proc.stdout == "" and not changed
    assert all(c[0] == "get" for c in state["calls"])


def test_the_request_joins_the_others_once(tmp_path):
    _, _, state = _run(tmp_path, _master(protected=True, actions=["UPDATE_PASSWORD"]))
    asked = ["UPDATE_PASSWORD", "CONFIGURE_TOTP"]
    assert state["master"]["users"][ADMIN]["requiredActions"] == asked
    proc, changed, state = _run(tmp_path, state["master"])
    assert state["master"]["users"][ADMIN]["requiredActions"] == asked
    assert proc.stdout == "" and not changed


def test_numbers_tuned_in_the_master_realm_stay(tmp_path):
    _, _, state = _run(tmp_path, _master(failureFactor=5, maxFailureWaitSeconds=300))
    assert state["master"]["failureFactor"] == 5
    assert state["master"]["maxFailureWaitSeconds"] == 300
    sets = [c[c.index("-s") + 1] for c in state["calls"] if c[0] == "update" and "-s" in c]
    assert "bruteForceProtected=true" in sets and not any("Failure" in s for s in sets)


def test_a_master_realm_without_the_admin_account_changes_only_the_lockout(tmp_path):
    proc, _, state = _run(tmp_path, _master(users=["someone@acme.test"]))
    assert state["master"]["bruteForceProtected"] is True
    assert state["master"]["users"]["someone@acme.test"]["requiredActions"] == []
    assert f"no account {ADMIN}" in proc.stderr


def _two_more() -> dict:
    master = _master(protected=True)
    _add(master, "recovery@acme.test")
    _add(master, "service-account-catena-automation")
    _add(master, "reader@acme.test", admin=False)
    return master


def test_with_the_setting_off_only_the_named_admin_is_asked(tmp_path):
    _, _, state = _run(tmp_path, _two_more(), enforce="false")
    users = state["master"]["users"]
    assert users[ADMIN]["requiredActions"] == ["CONFIGURE_TOTP"]
    assert users["recovery@acme.test"]["requiredActions"] == []


def _admins(tmp_path: Path) -> dict:
    return {a["name"]: a for a in json.loads((tmp_path / "admins.json").read_text())["admins"]}


def test_every_master_admin_is_recorded_for_the_panel(tmp_path):
    master = _two_more()
    _add(master, "coded@acme.test", credentials=("password", "otp"))
    _run(tmp_path, master, enforce="false")
    assert _admins(tmp_path) == {
        ADMIN: {"name": ADMIN, "otp": False, "asked": True},
        "recovery@acme.test": {"name": "recovery@acme.test", "otp": False, "asked": False},
        "coded@acme.test": {"name": "coded@acme.test", "otp": True, "asked": False}}
    assert (tmp_path / "admins.json").stat().st_mode & 0o777 == 0o644
    _run(tmp_path, master, enforce="true")
    assert _admins(tmp_path)["recovery@acme.test"]["asked"] is True


@pytest.mark.parametrize("on", ["true", "True", "yes"])
def test_with_the_setting_on_every_master_admin_is_asked(tmp_path, on):
    proc, changed, state = _run(tmp_path, _two_more(), enforce=on)
    users = state["master"]["users"]
    assert users[ADMIN]["requiredActions"] == ["CONFIGURE_TOTP"]
    assert users["recovery@acme.test"]["requiredActions"] == ["CONFIGURE_TOTP"]
    assert "asks for a second factor: recovery@acme.test" in proc.stdout and changed


@pytest.mark.parametrize("via", ["group", "composite"])
def test_an_admin_holding_the_role_through_a_group_or_a_composite_is_asked(tmp_path, via):
    master = _two_more()
    _add(master, "indirect@acme.test", via=via)
    proc, _, state = _run(tmp_path, master, enforce="true")
    users = state["master"]["users"]
    assert users["indirect@acme.test"]["requiredActions"] == ["CONFIGURE_TOTP"]
    assert "asks for a second factor: indirect@acme.test" in proc.stdout
    assert _admins(tmp_path)["indirect@acme.test"]["asked"] is True
    assert users["reader@acme.test"]["requiredActions"] == []
    # The direct mapping of an indirect holder lacks the admin role, so only the
    # effective (composite) endpoint can have found them.
    assert users["indirect@acme.test"]["admin"] is False
    assert any(c[1] == "users/u-indirect@acme.test/role-mappings/realm/composite"
               for c in state["calls"])


@pytest.mark.parametrize("fail", [{"composite": "recovery@acme.test"},
                                  {"update": "recovery@acme.test"}])
def test_a_failed_kcadm_call_fails_the_task_and_leaves_no_record(tmp_path, fail):
    record = tmp_path / "admins.json"
    record.write_text('{"checked_at":"2026-01-01T00:00:00Z","admins":[]}')
    _run(tmp_path, _two_more(), enforce="true", fail=fail, succeeds=False)
    assert not record.exists()


def test_the_record_is_dated(tmp_path):
    _run(tmp_path, _two_more(), enforce="true")
    stamp = json.loads((tmp_path / "admins.json").read_text())["checked_at"]
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")


def test_the_service_account_and_non_admins_are_left_alone(tmp_path):
    _, _, state = _run(tmp_path, _two_more(), enforce="true")
    users = state["master"]["users"]
    assert users["service-account-catena-automation"]["requiredActions"] == []
    assert users["reader@acme.test"]["requiredActions"] == []


def test_an_admin_with_a_code_or_the_request_is_not_asked_again(tmp_path):
    master = _two_more()
    _add(master, "coded@acme.test", credentials=("password", "otp"))
    _add(master, "asked@acme.test", actions=["CONFIGURE_TOTP"])
    _, _, state = _run(tmp_path, master, enforce="true")
    users = state["master"]["users"]
    assert users["coded@acme.test"]["requiredActions"] == []
    assert users["asked@acme.test"]["requiredActions"] == ["CONFIGURE_TOTP"]
    proc, changed, state = _run(tmp_path, state["master"], enforce="true")
    assert proc.stdout == "" and not changed
    assert state["master"]["users"]["recovery@acme.test"]["requiredActions"] == ["CONFIGURE_TOTP"]


def test_other_admins_are_asked_even_without_the_named_account(tmp_path):
    master = _master(protected=True, users=["other@acme.test"])
    proc, _, state = _run(tmp_path, master, enforce="true")
    assert f"no account {ADMIN}" in proc.stderr
    assert state["master"]["users"]["other@acme.test"]["requiredActions"] == ["CONFIGURE_TOTP"]


def test_the_lockout_numbers_are_the_vps_realms():
    """The numbers Keycloak gives the master realm when it creates it, which
    the vps realm's seed writes."""
    seed = yaml.safe_load(_render(realm_seed=True))
    assert {k: seed[k] for k in ("permanentLockout", "maxFailureWaitSeconds",
                                 "minimumQuickLoginWaitSeconds", "waitIncrementSeconds",
                                 "quickLoginCheckMilliSeconds", "maxDeltaTimeSeconds",
                                 "failureFactor")} == {
        "permanentLockout": False, "maxFailureWaitSeconds": 900,
        "minimumQuickLoginWaitSeconds": 60, "waitIncrementSeconds": 60,
        "quickLoginCheckMilliSeconds": 1000, "maxDeltaTimeSeconds": 43200,
        "failureFactor": 30}


@pytest.mark.parametrize("after", ["kcadm login to master",
                                   "clear is_temporary_admin marker"])
def test_it_runs_on_the_automation_clients_session_after_the_import(after):
    names = [str(t.get("name", "")) for t in yaml.safe_load(BOOTSTRAP.read_text())]
    step = names.index(f"Keycloak realm: {STEP}")
    assert next(i for i, n in enumerate(names) if after in n) < step
    script = _task()["ansible.builtin.shell"]
    assert "admin_password" not in script and "config credentials" not in script
