"""Every master-realm sign-in the tree makes but one is the automation
client's, with its secret on stdin, never the admin's password: the master
admin carries a second factor, which a kcadm password sign-in cannot answer
(CV2), and no credential rides an argv or a task environment, which Ansible
prefixes to its module's `/bin/sh -c` command line, both shown by `ps` to every
local user (CV7).

kcadm reads KC_CLI_CLIENT_SECRET when it is given a client and no secret, and
KC_CLI_PASSWORD when it is given a user and no password (BaseConfigCredentialsCmd,
Keycloak 26.6.7); keycloak-config-cli takes the client-credentials grant from
KEYCLOAK_GRANTTYPE, KEYCLOAK_CLIENTID and KEYCLOAK_CLIENTSECRET
(KeycloakConfigProperties, keycloak-config-cli 6.5.1). `docker exec -e NAME` and
`docker run -e NAME` forward a variable from the client's environment.

One task signs in with the admin's password: the one that creates the
automation client, or gives it the store's secret and the admin role, and only
after the client failed to sign in or to read the master realm. A refused
password counts toward the master realm's lockout (30 failures, two within a
second lock for a minute: DefaultBruteForceProtector, Keycloak 26.6.7), so it
tries each address once and a refusal ends its retries. Its script runs here
against a stand-in `docker`, and under the task's `retries` and `until`.

Run: uv run pytest tests/unit/test_master_realm_sign_ins_use_the_automation_client.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

from ansible_tree import ANSIBLE, PLAYBOOKS, ROLE_ROOTS, command_text, walk_tasks

ROLE = ANSIBLE / "reconcile" / "roles" / "keycloak"
TASKS = ROLE / "tasks"
BOOTSTRAP_STEP = "the automation client exists"
CLIENT_STDIN = "{{ keycloak_automation_client_secret }}"

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

CT = "keycloak_server.1.abc"
CLIENT = "catena-automation"
SECRET = "c1ient+sec/ret="
PASSWORD = "Adm1n-pass_word"
ADMIN = "admin@acme.test"


def _yml_files():
    for root in (*ROLE_ROOTS, PLAYBOOKS):
        yield from sorted(root.rglob("*.yml"))


def _signins():
    """(file, task, command text) of each task that signs in to Keycloak."""
    for path in _yml_files():
        doc = yaml.safe_load(path.read_text())
        if isinstance(doc, list) and doc and isinstance(doc[0], dict) and "hosts" in doc[0]:
            doc = [t for play in doc for key in ("pre_tasks", "tasks", "post_tasks")
                   for t in play.get(key) or []]
        for task in walk_tasks(doc if isinstance(doc, list) else []):
            text = command_text(task)
            if "config credentials" in text or "keycloak_config_cli_image" in text:
                yield path.relative_to(ANSIBLE), task, text


def _args(task: dict) -> dict:
    mod = task.get("ansible.builtin.command") or task.get("ansible.builtin.shell")
    return {**(mod if isinstance(mod, dict) else {}), **(task.get("args") or {})}


def test_there_are_sign_ins_to_check():
    names = {(str(path), task["name"]) for path, task, _ in _signins()}
    assert len(names) >= 3, names


def test_every_sign_in_but_the_clients_creation_is_the_client_s():
    for path, task, text in _signins():
        where = f"{path}: {task['name']!r}"
        assert "environment" not in task, where
        assert task.get("no_log") is True, where
        assert "--password" not in text, where
        if BOOTSTRAP_STEP in task["name"]:
            continue
        assert "admin_password" not in text and "--user" not in text, where
        assert _args(task).get("stdin") == CLIENT_STDIN, where
        assert "--client" in text or "KEYCLOAK_GRANTTYPE=client_credentials" in text, where


def test_only_the_clients_creation_reads_the_admin_password():
    readers = [(str(path), task["name"]) for path, task, _ in _signins()
               if "admin_password" in json.dumps(task)]
    assert readers == [("reconcile/roles/keycloak/tasks/realm_bootstrap.yml",
                        f"Keycloak realm: {BOOTSTRAP_STEP}")], readers


def test_no_playbook_signs_in_with_a_password_on_its_command_line():
    for path in _yml_files():
        text = path.read_text()
        assert "kcadm.sh" not in text or "--password" not in text, path


def test_the_import_signs_in_with_the_client_credentials_grant():
    argv = yaml.safe_load((TASKS / "_config_cli_import.yml").read_text())[0][
        "ansible.builtin.command"]["argv"]
    assert "KEYCLOAK_GRANTTYPE=client_credentials" in argv
    assert "KEYCLOAK_CLIENTID={{ keycloak_automation_client_id }}" in argv
    assert not any(str(a).startswith(("KEYCLOAK_USER", "KEYCLOAK_PASSWORD")) for a in argv)
    out = subprocess.run(argv[:4] + ["printenv", "KEYCLOAK_CLIENTSECRET"],
                         input=SECRET + "\n", text=True, capture_output=True, check=True)
    assert out.stdout == SECRET + "\n"


# --- the scripts, against a stand-in docker ------------------------------------

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ["FAKE_KC_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    state["argv"].append(args)
    def done(code=0):
        json.dump(state, open(path, "w"))
        sys.exit(code)
    if args[0] == "ps":
        # Each attempt looks the container up once: Keycloak answers from the
        # attempt after the `down` first ones.
        state["up"] = state["down"] <= 0
        state["down"] -= 1
        print("keycloak_server.1.abc")
        done()
    assert args[0] == "exec", args
    i = 1
    env = {}
    stdin = None
    while args[i].startswith("-"):
        if args[i] == "-e":
            env[args[i + 1]] = os.environ.get(args[i + 1])
            i += 2
        elif args[i] == "-i":
            stdin = sys.stdin.read()
            i += 1
    assert args[i] == "keycloak_server.1.abc", args
    if args[i + 1] == "bash":
        assert args[i + 2] == "-c" and "9000" in args[i + 3] and "/health/ready" in args[i + 3], args
        done(0 if state["up"] else 1)
    assert args[i + 1] == "/opt/keycloak/bin/kcadm.sh", args
    kc = args[i + 2:]
    def opt(name):
        return kc[kc.index(name) + 1] if name in kc else None
    if kc[:2] == ["config", "credentials"]:
        assert opt("--realm") == "master" and opt("--server") == "http://localhost:8080", kc
        name = opt("--client") or opt("--user")
        if not state["up"]:
            state["logins"].append([name, None])
            done(1)
        client = state["clients"].get(opt("--client") or "")
        if opt("--client"):
            ok = client is not None and env.get("KC_CLI_CLIENT_SECRET") == client["secret"]
            if ok:
                state["session"], state["session_client"] = "client", name
        else:
            ok = (opt("--user") in state["users"] and not state["second_factor"]
                  and env.get("KC_CLI_PASSWORD") == state["password"])
            state["session"] = "admin" if ok else state["session"]
        state["logins"].append([name, ok])
        done(0 if ok else 1)
    assert state["session"] in ("admin", "client"), kc
    if kc[:2] == ["get", "realms/master"]:
        assert opt("--fields") == "realm", kc
        if state["session"] == "client" and "admin" not in state["clients"][state["session_client"]]["roles"]:
            done("HTTP 403 Forbidden")
        print("master")
        done()
    if kc[:2] == ["get", "clients"]:
        cid = [q.split("=", 1)[1] for q in kc if q.startswith("clientId=")][0]
        if cid in state["clients"]:
            print(state["clients"][cid]["id"])
        done()
    if kc[:2] == ["create", "clients"]:
        assert opt("-f") == "-" and stdin is not None, kc
        rep = json.loads(stdin)
        state["clients"][rep["clientId"]] = dict(rep, id="id-" + rep["clientId"], roles=[])
        done()
    if kc[0] == "update" and kc[1].startswith("clients/"):
        assert opt("-f") == "-" and "-m" in kc and stdin is not None, kc
        rep = json.loads(stdin)
        client = next(c for c in state["clients"].values() if c["id"] == kc[1].split("/")[1])
        client.update(rep)
        done()
    if kc[0] == "add-roles":
        assert opt("-r") == "master" and opt("--rolename") == "admin", kc
        name = opt("--uusername")[len("service-account-"):]
        state["clients"][name]["roles"].append("admin")
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _realm(clients=None, users=(ADMIN,), second_factor=False, password=PASSWORD, down=0):
    return {"clients": clients or {}, "users": list(users), "password": password,
            "second_factor": second_factor, "down": down, "up": True, "session": None,
            "logins": [], "argv": []}


def _template(text: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(text))


def _script(task: dict, **extra) -> str:
    variables = {
        "keycloak_compose_name": "keycloak", "keycloak_server_service": "server",
        "keycloak_internal_port": 8080, "keycloak_management_port": 9000,
        "keycloak_health_path": "/health/ready", "keycloak_automation_client_id": CLIENT,
        "admin_email": ADMIN, "_kc_recorded_admin_email": "", **extra,
    }
    return _template(task["ansible.builtin.shell"], variables)


def _run(tmp_path: Path, task: dict, realm: dict, stdin: str, **extra):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(realm))
    proc = subprocess.run(
        ["bash", "-c", _script(task, **extra)], input=stdin, text=True, capture_output=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
    return proc, json.loads(state.read_text())


def _named(path: Path, fragment: str) -> dict:
    return next(t for t in walk_tasks(yaml.safe_load(path.read_text()))
                if fragment in str(t.get("name", "")))


def _bootstrap():
    return _named(TASKS / "realm_bootstrap.yml", BOOTSTRAP_STEP)


def _bootstrap_stdin(secret=SECRET, password=PASSWORD) -> str:
    task = _bootstrap()
    assert task["args"]["stdin"] == CLIENT_STDIN + "\n{{ admin_password }}"
    return f"{secret}\n{password}\n"


def _no_credential_in_an_argv(state: dict):
    for argv in state["argv"]:
        joined = " ".join(argv)
        assert SECRET not in joined and PASSWORD not in joined, argv


def _retried(tmp_path: Path, realm: dict, stdin: str, **extra):
    """The bootstrap step as Ansible runs it against one Keycloak: again while
    `until` is false, up to `retries` more times."""
    task = _bootstrap()
    for _ in range(task["retries"] + 1):
        proc, realm = _run(tmp_path, task, realm, stdin, **extra)
        if _template("{{ " + task["until"] + " }}", {task["register"]: {"rc": proc.returncode}}):
            break
    return proc, realm


def _shared_sign_in():
    return _named(TASKS / "_master_login.yml", "signs in to the master realm")


def test_the_sign_in_puts_the_secret_on_no_argv_and_names_its_container(tmp_path):
    task = _shared_sign_in()
    assert task["args"]["stdin"] == CLIENT_STDIN
    realm = _realm(clients={CLIENT: {"id": "c1", "secret": SECRET, "roles": ["admin"]}})
    proc, state = _run(tmp_path, task, realm, SECRET + "\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == CT + "\n"
    assert state["logins"] == [[CLIENT, True]]
    _no_credential_in_an_argv(state)


@pytest.mark.parametrize("realm", [
    _realm(clients={CLIENT: {"id": "c1", "secret": "old", "roles": ["admin"]}}),
    _realm(clients={CLIENT: {"id": "c1", "secret": SECRET, "roles": ["admin"]}}, down=1),
], ids=["refused", "not-ready"])
def test_the_sign_in_fails_when_kcadm_s_does(tmp_path, realm):
    """Its rc is what `until` retries on and what an optional caller reads."""
    proc, state = _run(tmp_path, _shared_sign_in(), realm, SECRET + "\n")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert len(state["logins"]) == 1 and not state["logins"][0][1]


@pytest.mark.parametrize("variables, ignored", [
    ({}, False), ({"kc_master_login_optional": True}, True)])
def test_an_optional_sign_in_leaves_its_failure_to_the_caller(variables, ignored):
    """Retries that run out fail the task whatever failed_when says
    (TaskExecutor._execute, ansible-core)."""
    task = _shared_sign_in()
    assert "failed_when" not in task
    assert _template(task["ignore_errors"], variables) is ignored


def test_a_client_that_signs_in_needs_no_password(tmp_path):
    realm = _realm(clients={CLIENT: {"id": "c1", "secret": SECRET, "roles": ["admin"]}})
    proc, state = _run(tmp_path, _bootstrap(), realm, _bootstrap_stdin())
    assert proc.returncode == 0, proc.stderr
    assert state["logins"] == [[CLIENT, True]]
    assert proc.stdout == ""


def test_a_missing_client_is_created_with_the_stores_secret_and_the_admin_role(tmp_path):
    proc, state = _run(tmp_path, _bootstrap(), _realm(), _bootstrap_stdin())
    assert proc.returncode == 0, proc.stderr
    client = state["clients"][CLIENT]
    assert client["secret"] == SECRET and client["roles"] == ["admin"]
    assert client["serviceAccountsEnabled"] is True and client["publicClient"] is False
    for flow in ("standardFlowEnabled", "implicitFlowEnabled", "directAccessGrantsEnabled"):
        assert client[flow] is False, flow
    assert state["logins"] == [[CLIENT, False], [ADMIN, True], [CLIENT, True]]
    assert state["session"] == "client"
    assert f"created: {CLIENT}" in proc.stdout
    _no_credential_in_an_argv(state)


def test_a_client_holding_another_secret_gets_the_stores(tmp_path):
    realm = _realm(clients={CLIENT: {"id": "c1", "secret": "old", "roles": []}})
    proc, state = _run(tmp_path, _bootstrap(), realm, _bootstrap_stdin())
    assert proc.returncode == 0, proc.stderr
    assert state["clients"][CLIENT]["secret"] == SECRET
    assert state["clients"][CLIENT]["roles"] == ["admin"]
    assert f"secret reset: {CLIENT}" in proc.stdout


def test_the_email_the_last_import_ran_under_signs_in_while_unmoved(tmp_path):
    old = "old@acme.test"
    proc, state = _run(tmp_path, _bootstrap(), _realm(users=[old]), _bootstrap_stdin(),
                       _kc_recorded_admin_email=old)
    assert proc.returncode == 0, proc.stderr
    assert state["logins"] == [[CLIENT, False], [ADMIN, False], [old, True], [CLIENT, True]]


def test_a_client_without_the_admin_role_gets_it_back(tmp_path):
    """It signs in but cannot read the master realm, so every later step would
    be refused."""
    realm = _realm(clients={CLIENT: {"id": "c1", "secret": SECRET, "roles": []}})
    proc, state = _run(tmp_path, _bootstrap(), realm, _bootstrap_stdin())
    assert proc.returncode == 0, proc.stderr
    assert state["clients"][CLIENT]["roles"] == ["admin"]
    assert state["logins"] == [[CLIENT, True], [ADMIN, True], [CLIENT, True]]
    assert any(a[-4:] == ["get", "realms/master", "--fields", "realm"] for a in state["argv"])


@pytest.mark.parametrize("realm", [_realm(second_factor=True), _realm(users=["other@acme.test"])],
                         ids=["second-factor", "no-account"])
def test_without_the_client_or_the_password_the_step_fails_and_changes_nothing(tmp_path, realm):
    proc, state = _run(tmp_path, _bootstrap(), realm, _bootstrap_stdin())
    assert proc.returncode == 3
    assert "cannot manage the master realm" in proc.stderr
    assert state["clients"] == {}


def test_an_unmoved_email_equal_to_admin_email_is_tried_once(tmp_path):
    proc, state = _run(tmp_path, _bootstrap(), _realm(password="changed"), _bootstrap_stdin(),
                       _kc_recorded_admin_email=ADMIN)
    assert proc.returncode == 3
    assert state["logins"] == [[CLIENT, False], [ADMIN, False]]


def test_a_refused_password_ends_the_retries(tmp_path):
    """One refused password per address per converge, against 30 that lock the
    account."""
    proc, state = _retried(tmp_path, _realm(password="changed"), _bootstrap_stdin(),
                           _kc_recorded_admin_email=ADMIN)
    assert proc.returncode == 3
    assert state["logins"] == [[CLIENT, False], [ADMIN, False]]


def test_a_keycloak_still_starting_is_waited_out_and_refuses_no_password(tmp_path):
    proc, state = _retried(tmp_path, _realm(down=2), _bootstrap_stdin())
    assert proc.returncode == 0, proc.stderr
    assert state["clients"][CLIENT]["roles"] == ["admin"]
    assert state["logins"] == [[CLIENT, None], [ADMIN, None]] * 2 + [
        [CLIENT, False], [ADMIN, True], [CLIENT, True]]


@pytest.mark.parametrize("realm, why, last_error", [
    (_realm(second_factor=True), "cannot manage Keycloak's master realm",
     "cannot manage the master realm"),
    (_realm(down=99), "ran out of retries", "keycloak is not ready"),
], ids=["refused", "retries-run-out"])
def test_a_step_that_cannot_make_the_client_stops_the_converge_and_says_why(
        tmp_path, realm, why, last_error):
    """no_log hides the step's own failure, so the task after it reports the
    last attempt's stderr, which carries neither credential. Retries that run
    out mark the step failed whatever failed_when says (TaskExecutor._execute,
    ansible-core), so the step ignores its error to reach that task."""
    tasks = yaml.safe_load((TASKS / "realm_bootstrap.yml").read_text())
    names = [str(t.get("name", "")) for t in tasks]
    step = next(i for i, n in enumerate(names) if BOOTSTRAP_STEP in n)
    assert tasks[step]["ignore_errors"] is True and tasks[step]["no_log"] is True
    stop = tasks[step + 1]
    assert "ansible.builtin.fail" in stop and "no_log" not in stop

    proc, _ = _retried(tmp_path, realm, _bootstrap_stdin())
    variables = {"keycloak_automation_client_id": CLIENT, tasks[step]["register"]: {
        "rc": proc.returncode, "stderr_lines": proc.stderr.splitlines(), "failed": True}}
    assert _template("{{ " + stop["when"] + " }}", variables) is True
    for name, value in stop["vars"].items():
        variables[name] = _template(value, variables)
    msg = json.dumps([_template(line, variables) for line in stop["ansible.builtin.fail"]["msg"]])
    assert why in msg and last_error in msg
    assert SECRET not in msg and PASSWORD not in msg


def test_the_client_exists_before_any_session_uses_it():
    names = [str(t.get("name", "")) for t in
             yaml.safe_load((TASKS / "realm_bootstrap.yml").read_text())]
    first = next(i for i, n in enumerate(names) if BOOTSTRAP_STEP in n)
    login = next(i for i, n in enumerate(names) if "sign kcadm in to the master realm" in n)
    for later in ("move the admin's accounts", "list the realms", "seed the admin password",
                  "import the realm config", "kcadm login to master",
                  "asks its admin for a second factor"):
        assert first < login < next(i for i, n in enumerate(names) if later in n), later
    keep = next(i for i, n in enumerate(names) if "keep the admin email" in n)
    assert keep < first


def test_every_other_master_realm_session_is_the_shared_sign_in():
    for path in sorted(TASKS.glob("*.yml")):
        for task in walk_tasks(yaml.safe_load(path.read_text())):
            if "kcadm login to master" in str(task.get("name", "")):
                assert task["ansible.builtin.include_tasks"]["file"] == "_master_login.yml", path
    play = yaml.safe_load((PLAYBOOKS / "validate.yml").read_text())
    included = [t["ansible.builtin.include_role"] for p in play for t in p.get("tasks") or []
                if "ansible.builtin.include_role" in t
                and t["ansible.builtin.include_role"].get("tasks_from") == "_master_login"]
    assert included == [{"name": "keycloak", "tasks_from": "_master_login"}]


def test_the_shared_sign_in_reruns_its_lookup_and_can_be_left_to_the_caller():
    task = _named(TASKS / "_master_login.yml", "signs in to the master realm")
    assert "docker ps" in task["ansible.builtin.shell"]
    assert task["until"] == "_kc_master_login.rc == 0"
    export = _named(TASKS / "realm_export.yml", "kcadm login to master")
    assert export["vars"] == {"kc_master_login_optional": True}
    assert os.path.basename(export["ansible.builtin.include_tasks"]["file"]) == "_master_login.yml"
