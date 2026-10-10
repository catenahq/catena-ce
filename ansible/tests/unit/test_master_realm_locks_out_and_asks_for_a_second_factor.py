"""The master realm's sign-in locks out repeated wrong passwords as the vps
realm's does, and its admin must set up and use a second factor (CV2).

Keycloak creates every realm with brute-force protection off and the numbers
the vps realm's seed repeats: a 60-second pause growing by 60 seconds per
failure, capped at 900 (RealmManager setupRealmDefaults, Keycloak 26.6.7). So
the converge switches the protection on in the master realm and keeps numbers
tuned there, and asks the master admin to set up a one-time code
(CONFIGURE_TOTP) while they hold none; once they hold one, the master realm's
browser flow asks for it at every sign-in. The step's script runs here against
a stand-in `docker` that answers the kcadm calls from a JSON state file.

Run: uv run pytest tests/unit/test_master_realm_locks_out_and_asks_for_a_second_factor.py
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
        user["requiredActions"].append("CONFIGURE_TOTP")
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _task() -> dict:
    return next(t for t in yaml.safe_load(BOOTSTRAP.read_text())
                if STEP in str(t.get("name", "")))


def _master(protected=False, credentials=("password",), actions=(), users=(ADMIN,), **extra):
    return {"bruteForceProtected": protected, "failureFactor": 30, **extra,
            "users": {u: {"credentials": list(credentials), "requiredActions": list(actions)}
                      for u in users}}


def _run(tmp_path: Path, master: dict):
    task = _task()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"master": master, "calls": []}))
    variables = {"_keycloak_realm_server_ct": {"stdout": CT}, "admin_email": ADMIN}
    script = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(task["ansible.builtin.shell"]))
    proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    assert proc.returncode == 0, proc.stderr
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
