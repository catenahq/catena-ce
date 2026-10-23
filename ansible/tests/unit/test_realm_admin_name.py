"""The administrator's first and last name reach the admin's account in the master
realm and in the vps realm, while the account's name is empty.

The step's script runs here against a stand-in `docker` that answers the kcadm
calls from a JSON state file and refuses any other call. The names reach the
script over stdin, as they do in Ansible.

Run: uv run pytest tests/unit/test_realm_admin_name.py
"""
from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ANSIBLE = Path(__file__).resolve().parents[2]
BOOTSTRAP = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "realm_bootstrap.yml"

CT = "keycloak_server.1.abc"
ADMIN = "admin@acme.test"
NAME_STEP = "the administrator's accounts carry the administrator's name"
WARN_STEP = "a realm refused the administrator's name"

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ["FAKE_KC_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    assert args[:4] == ["exec", "-i", "keycloak_server.1.abc", "/opt/keycloak/bin/kcadm.sh"], args
    kc = args[4:]
    state["calls"].append(kc)
    def opt(name):
        return kc[kc.index(name) + 1]
    def done(code=0):
        json.dump(state, open(path, "w"))
        sys.exit(code)
    realm = opt("-r")
    users = state["realms"][realm]["users"]
    verb, uri = kc[0], kc[1]
    if verb == "get" and uri == "users":
        queries = [kc[i + 1] for i, a in enumerate(kc) if a == "-q"]
        assert "exact=true" in queries and opt("--fields") == "id", kc
        name = next(q.split("=", 1)[1] for q in queries if q.startswith("username="))
        if name in users:
            print(users[name]["id"])
        done()
    if verb == "get" and uri.startswith("users/"):
        assert opt("--fields") == "firstName,lastName", kc
        user = next(u for u in users.values() if u["id"] == uri.split("/")[1])
        print(json.dumps({k: user[k] for k in ("firstName", "lastName") if k in user}))
        done()
    if verb == "update" and uri.startswith("users/"):
        assert "-m" in kc and opt("-f") == "-", kc
        body = json.load(sys.stdin)
        state["bodies"].append([realm, body])
        if realm in state["refuse"]:
            print("Error: user profile refused the value", file=sys.stderr)
            done(1)
        user = next(u for u in users.values() if u["id"] == uri.split("/")[1])
        user.update(body)
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _task(fragment: str) -> dict:
    return next(t for t in yaml.safe_load(BOOTSTRAP.read_text())
                if isinstance(t, dict) and fragment in str(t.get("name", "")))


def _variables(task: dict, first="Alex", last="Tremblay", **extra) -> dict:
    return {"_keycloak_realm_server_ct": {"stdout": CT}, "keycloak_realm": "vps",
            "admin_email": ADMIN, "admin_first_name": first, "admin_last_name": last,
            **extra}


def _template(text: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text))


def _account(uid: str, **names) -> dict:
    return {"id": uid, **names}


def _state(master=None, vps=None, refuse=()) -> dict:
    return {"realms": {"master": {"users": master if master is not None else {ADMIN: _account("m1")}},
                       "vps": {"users": vps if vps is not None else {ADMIN: _account("v1")}}},
            "refuse": list(refuse), "calls": [], "bodies": []}


def _run(tmp_path: Path, state: dict, first="Alex", last="Tremblay"):
    """The step as Ansible runs it: (exit code, stdout, stderr, changed) and the
    state after. A step whose `when` is false is not run: stdout is None."""
    task = _task(NAME_STEP)
    variables = _variables(task, first, last)
    if not all(_template("{{ " + w + " }}", variables) for w in task["when"]):
        return (0, None, "", False), state
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    script = _template(task["ansible.builtin.shell"], variables)
    stdin = _template(task["args"]["stdin"], variables) + "\n"  # stdin_add_newline
    proc = subprocess.run(
        ["bash", "-c", script], input=stdin, text=True, capture_output=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(path)})
    changed = _template("{{ " + task["changed_when"] + " }}",
                        {**variables, task["register"]: {"stdout": proc.stdout}})
    return (proc.returncode, proc.stdout, proc.stderr, changed), json.loads(path.read_text())


def test_the_step_runs_after_the_admin_rejoin_and_before_the_flow_ids():
    names = [str(t.get("name", "")) for t in yaml.safe_load(BOOTSTRAP.read_text())
             if isinstance(t, dict)]
    at = lambda fragment: next(i for i, n in enumerate(names) if fragment in n)  # noqa: E731
    assert at("an empty /admin gets the realm admin back") < at(NAME_STEP) < at("read the sign-in flow ids")


def test_empty_names_are_written_once_in_both_realms_over_stdin(tmp_path):
    (code, out, _, changed), state = _run(tmp_path, _state())
    assert code == 0 and changed
    assert out == "named: master\nnamed: vps\n"
    for realm, uid in (("master", "m1"), ("vps", "v1")):
        user = state["realms"][realm]["users"][ADMIN]
        assert (user["firstName"], user["lastName"]) == ("Alex", "Tremblay"), realm
    assert state["bodies"] == [["master", {"firstName": "Alex", "lastName": "Tremblay"}],
                               ["vps", {"firstName": "Alex", "lastName": "Tremblay"}]]
    assert not any("Alex" in arg for call in state["calls"] for arg in call), "a name reached argv"

    (_, out, _, changed), after = _run(tmp_path, state)
    assert out == "" and not changed
    assert len(after["bodies"]) == 2


def test_a_name_set_in_keycloak_is_left_alone(tmp_path):
    state = _state(master={ADMIN: _account("m1", firstName="Sam", lastName="Roy")},
                   vps={ADMIN: _account("v1", firstName="Sam", lastName="Roy")})
    (code, out, _, changed), after = _run(tmp_path, state)
    assert code == 0 and out == "" and not changed
    assert after["bodies"] == []


def test_only_the_empty_name_is_written(tmp_path):
    state = _state(master={ADMIN: _account("m1", firstName="Sam", lastName="")},
                   vps={ADMIN: _account("v1", lastName="Roy")})
    _, after = _run(tmp_path, state)
    assert after["bodies"] == [["master", {"lastName": "Tremblay"}],
                               ["vps", {"firstName": "Alex"}]]
    assert after["realms"]["master"]["users"][ADMIN]["firstName"] == "Sam"
    assert after["realms"]["vps"]["users"][ADMIN]["lastName"] == "Roy"


def test_a_name_with_quotes_arrives_whole(tmp_path):
    _, state = _run(tmp_path, _state(), first='Ann "Mo"', last="O'Neil <x>")
    assert state["bodies"][0][1] == {"firstName": 'Ann "Mo"', "lastName": "O'Neil <x>"}


def test_a_store_without_names_writes_nothing(tmp_path):
    for first, last in (("", ""), ("Alex", ""), ("", "Tremblay")):
        (code, out, _, _), state = _run(tmp_path, _state(), first, last)
        assert code == 0 and out is None
        assert state["calls"] == [] and state["bodies"] == []


def test_a_realm_without_the_account_is_skipped(tmp_path):
    (code, out, _, _), state = _run(tmp_path, _state(vps={}))
    assert code == 0 and out == "named: master\n"


def test_a_refused_name_is_a_warning_and_the_other_realm_is_still_named(tmp_path):
    (code, out, err, changed), state = _run(tmp_path, _state(refuse=["vps"]))
    assert code == 0 and "refused: vps" in out and "named: master" in out and changed
    assert "refused: vps: Error: user profile refused the value" in out
    assert "firstName" not in state["realms"]["vps"]["users"][ADMIN]

    warn = _task(WARN_STEP)
    shown = _template(
        "{{ " + warn["loop"].strip("{} ") + " }}",
        {"_keycloak_admin_name": {"stdout_lines": out.splitlines()}})
    assert shown == ["refused: vps: Error: user profile refused the value"]
