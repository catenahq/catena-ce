"""After the realm import, the converge adds what Catena needs in the realm and
removes nothing, so an edit a person makes in Keycloak survives it.

keycloak-config-cli makes exact what a realm file declares inside an object: a
declared role's composites (RealmRoleCompositeImportService), a declared group's
role mappings (GroupImportService updateGroupRealmRoles), the realm's default
client scope list (ClientScopeImportService updateDefaultClientScopes), a
declared user's fields. So realm-vps.yaml.j2 declares none of these, the realm
admin only in the seed, and three kcadm steps in realm_bootstrap.yml add what
Catena needs:

  - each tier role the sign-in flows check exists and is held by its group;
  - the realm's default client scopes hold the ones applications need;
  - an /admin left with no member gets the realm admin back.

Each step's script runs here against a stand-in `docker` that answers the kcadm
calls from a JSON state file and refuses any other call.

Run: uv run pytest tests/unit/test_realm_add_only_steps.py
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

from test_realm_bootstrap_settles import _render
from test_realm_signin_flows import ADMITTED

ANSIBLE = Path(__file__).resolve().parents[2]
BOOTSTRAP = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "realm_bootstrap.yml"

CT = "keycloak_server.1.abc"
ADMIN = "admin@acme.test"
TIERS = {"/admin": "catena-tier-admin", "/staff": "catena-tier-staff"}
NEEDED_SCOPES = ["groups", "profile", "email", "roles", "web-origins", "basic", "acr"]
TIER_STEP = "each tier group holds its tier role"
SCOPE_STEP = "the default client scopes hold the ones applications need"
ADMIN_STEP = "an empty /admin gets the realm admin back"

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
        return kc[kc.index(name) + 1]
    def done(code=0):
        json.dump(state, open(path, "w"))
        sys.exit(code)
    def group(gid):
        return next(g for g in state["groups"].values() if g["id"] == gid)
    assert opt("-r") == "vps", kc
    verb, uri = kc[0], kc[1]
    if verb == "get" and uri.startswith("roles/"):
        done(0 if uri[len("roles/"):] in state["roles"] else 1)
    if verb == "create" and uri == "roles":
        fields = dict(kc[i + 1].split("=", 1) for i, a in enumerate(kc) if a == "-s")
        state["roles"][fields["name"]] = fields["description"]
        done()
    if verb == "get-roles":
        assert opt("--fields") == "name" and opt("--format") == "csv" and "--noquotes" in kc
        print("\\n".join(state["groups"][opt("--gpath")]["roles"]))
        done()
    if verb == "add-roles":
        assert opt("--rolename") in state["roles"], kc
        state["groups"][opt("--gpath")]["roles"].append(opt("--rolename"))
        done()
    if verb == "get" and uri == "default-default-client-scopes":
        assert opt("--fields") == "name"
        print("\\n".join(state["defaults"]))
        done()
    if verb == "get" and uri == "client-scopes":
        assert opt("--fields") == "id,name"
        print("\\n".join(f"{sid},{name}" for name, sid in state["scopes"].items()))
        done()
    if verb == "update" and uri.startswith("default-default-client-scopes/"):
        sid = uri.rsplit("/", 1)[1]
        state["defaults"].append(next(n for n, i in state["scopes"].items() if i == sid))
        done()
    if verb == "get" and uri.startswith("group-by-path/"):
        print(state["groups"]["/" + uri[len("group-by-path/"):]]["id"])
        done()
    if verb == "get" and uri.startswith("groups/") and uri.endswith("/members"):
        members = group(uri.split("/")[1])["members"]
        print("\\n".join(members[:int(opt("-l"))]))
        done()
    if verb == "get" and uri == "users":
        queries = [kc[i + 1] for i, a in enumerate(kc) if a == "-q"]
        assert "exact=true" in queries, kc
        name = next(q.split("=", 1)[1] for q in queries if q.startswith("username="))
        if name in state["users"]:
            print(state["users"][name])
        done()
    if verb == "update" and uri.startswith("users/") and "/groups/" in uri:
        _, uid, _, gid = uri.split("/")
        group(gid)["members"].append(uid)
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(tasks: list[dict], fragment: str) -> int:
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _task(fragment: str) -> dict:
    tasks = _tasks()
    return tasks[_index(tasks, fragment)]


def _variables(task: dict, **extra) -> dict:
    return {"_keycloak_realm_server_ct": {"stdout": CT}, "keycloak_realm": "vps",
            "admin_email": ADMIN, **(task.get("vars") or {}), **extra}


def _changed(task: dict, stdout: str) -> bool:
    """The task's own changed_when, evaluated on one run's stdout."""
    variables = _variables(task, **{task["register"]: {"stdout": stdout}})
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template("{{ " + task["changed_when"] + " }}"))


def _realm(groups=None, **state) -> dict:
    groups = groups or {}
    return {
        "roles": state.get("roles", {}),
        "groups": {path: {"id": "g" + path.strip("/"), "roles": [], "members": [],
                          **groups.get(path, {})}
                   for path in ("/admin", "/staff", "/client", "/visitor")},
        "users": state.get("users", {}),
        "scopes": state.get("scopes", {}),
        "defaults": state.get("defaults", []),
        "calls": [],
    }


def _run(tmp_path: Path, fragment: str, realm: dict, check=True):
    """Runs one step once per loop item, as Ansible does; returns each run's
    (exit code, stdout, stderr, changed) and the realm state after."""
    task = _task(fragment)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(realm))
    runs = []
    for item in task.get("loop") or [None]:
        script = Templar(loader=DataLoader(), variables=_variables(task, item=item)).template(
            trust_as_template(task["ansible.builtin.shell"]))
        proc = subprocess.run(
            ["bash", "-c", script], text=True, capture_output=True,
            env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
        if check:
            assert proc.returncode == 0, proc.stderr
        runs.append((proc.returncode, proc.stdout, proc.stderr, _changed(task, proc.stdout)))
    return runs, json.loads(state.read_text())


def _verbs(state: dict) -> set[str]:
    return {call[0] for call in state["calls"]}


# --- what the realm file leaves to these steps -------------------------------

def test_the_persistent_realm_file_declares_none_of_it():
    realm = yaml.safe_load(_render(realm_seed=False))
    for key in ("roles", "defaultDefaultClientScopes", "users", "displayName",
                "displayNameHtml"):
        assert key not in realm, f"{key} is asserted on every converge"
    assert all("realmRoles" not in g and "clientRoles" not in g for g in realm["groups"])
    assert realm["enabled"] is True


def test_the_seed_creates_the_realm_admin_in_admin_with_its_password():
    [admin] = yaml.safe_load(_render(realm_seed=True))["users"]
    assert admin["username"] == admin["email"] == ADMIN
    assert admin["enabled"] is True and admin["emailVerified"] is True
    assert admin["groups"] == ["/admin"]
    assert admin["credentials"][0]["type"] == "password"


def test_the_steps_run_on_the_kcadm_session_after_the_import_and_before_the_flow_ids():
    tasks = _tasks()
    imported = _index(tasks, "import the realm config")
    login = _index(tasks, "kcadm login to master")
    flows = _index(tasks, "read the sign-in flow ids")
    for fragment in (TIER_STEP, SCOPE_STEP, ADMIN_STEP):
        assert imported < login < _index(tasks, fragment) < flows, fragment


# --- tier roles ----------------------------------------------------------------

def test_a_new_realm_gets_both_tier_roles_on_their_groups(tmp_path):
    runs, state = _run(tmp_path, TIER_STEP, _realm())
    assert set(state["roles"]) == set(TIERS.values())
    assert all(state["roles"].values()), "a tier role was created without a description"
    assert {path: state["groups"][path]["roles"] for path in TIERS} == {
        path: [role] for path, role in TIERS.items()}
    assert all(changed for *_, changed in runs)


def test_hand_made_mappings_stay_and_a_missing_tier_role_comes_back(tmp_path):
    roles = {"catena-tier-admin": "edited by hand", "catena-tier-staff": "x", "finance": ""}
    realm = _realm(roles=roles, groups={"/admin": {"roles": ["finance", "catena-tier-admin"]},
                                        "/staff": {"roles": ["finance"]}})
    runs, state = _run(tmp_path, TIER_STEP, realm)
    assert state["roles"] == roles, "an existing role was recreated or changed"
    assert state["groups"]["/admin"]["roles"] == ["finance", "catena-tier-admin"]
    assert state["groups"]["/staff"]["roles"] == ["finance", "catena-tier-staff"]
    assert [changed for *_, changed in runs] == [False, True]


def test_a_settled_realm_only_reads_its_tier_roles(tmp_path):
    realm = _realm(roles={role: "" for role in TIERS.values()},
                   groups={path: {"roles": [role]} for path, role in TIERS.items()})
    runs, state = _run(tmp_path, TIER_STEP, realm)
    assert not any(changed for *_, changed in runs)
    assert _verbs(state) == {"get", "get-roles"}


def test_the_roles_are_the_ones_the_sign_in_flows_check():
    loop = _task(TIER_STEP)["loop"]
    assert {f"/{i['group']}": i["role"] for i in loop} == TIERS
    assert {i["role"] for i in loop} == set().union(*ADMITTED.values())


# --- default client scopes ------------------------------------------------------

def _scopes(*names: str) -> dict:
    return {name: f"s-{name}" for name in names}


def test_the_needed_scopes_are_groups_and_keycloaks_openid_defaults():
    assert _task(SCOPE_STEP)["vars"]["_keycloak_needed_scopes"] == NEEDED_SCOPES


def test_a_new_realm_gains_groups_and_keeps_keycloaks_other_defaults(tmp_path):
    """A realm Keycloak creates lists its own defaults, SAML's role_list among
    them; the import has just created the groups scope."""
    keycloak = ["profile", "email", "roles", "web-origins", "basic", "acr", "role_list"]
    realm = _realm(scopes=_scopes(*keycloak, "groups", "phone"), defaults=list(keycloak))
    [(_, out, _, changed)], state = _run(tmp_path, SCOPE_STEP, realm)
    assert sorted(state["defaults"]) == sorted(keycloak + ["groups"])
    assert out == "added: groups\n" and changed


def test_a_scope_added_by_hand_stays_and_one_taken_off_comes_back(tmp_path):
    held = [s for s in NEEDED_SCOPES if s != "email"] + ["phone"]
    realm = _realm(scopes=_scopes(*NEEDED_SCOPES, "phone"), defaults=list(held))
    [(_, out, _, changed)], state = _run(tmp_path, SCOPE_STEP, realm)
    assert sorted(state["defaults"]) == sorted(held + ["email"])
    assert out == "added: email\n" and changed


def test_a_settled_realm_only_reads_its_default_scopes(tmp_path):
    realm = _realm(scopes=_scopes(*NEEDED_SCOPES), defaults=list(NEEDED_SCOPES))
    [(_, out, _, changed)], state = _run(tmp_path, SCOPE_STEP, realm)
    assert out == "" and not changed
    assert _verbs(state) == {"get"}


def test_a_needed_scope_missing_from_the_realm_fails_the_step(tmp_path):
    realm = _realm(scopes=_scopes(*[s for s in NEEDED_SCOPES if s != "acr"]),
                   defaults=[s for s in NEEDED_SCOPES if s != "acr"])
    [(code, _, err, _)], state = _run(tmp_path, SCOPE_STEP, realm, check=False)
    assert code != 0 and "client scope acr is missing" in err
    assert "update" not in _verbs(state)


# --- the realm admin -------------------------------------------------------------

@pytest.mark.parametrize("members", [["u-someone-else"], ["u-admin", "u-other"]])
def test_an_admin_group_with_a_member_is_left_alone(tmp_path, members):
    """The realm admin taken out of /admin by hand stays out while anybody is in."""
    realm = _realm(users={ADMIN: "u-admin"}, groups={"/admin": {"members": members}})
    [(_, out, _, changed)], state = _run(tmp_path, ADMIN_STEP, realm)
    assert state["groups"]["/admin"]["members"] == members
    assert out == "" and not changed
    assert _verbs(state) == {"get"}


def test_an_empty_admin_group_gets_the_realm_admin_back(tmp_path):
    realm = _realm(users={ADMIN: "u-admin", "other@acme.test": "u-other"})
    [(_, out, _, changed)], state = _run(tmp_path, ADMIN_STEP, realm)
    assert state["groups"]["/admin"]["members"] == ["u-admin"]
    assert out == f"joined: {ADMIN}\n" and changed


def test_an_empty_admin_group_without_the_account_changes_nothing(tmp_path):
    realm = _realm(users={"other@acme.test": "u-other"})
    [(code, out, err, changed)], state = _run(tmp_path, ADMIN_STEP, realm)
    assert code == 0 and not changed
    assert state["groups"]["/admin"]["members"] == []
    assert f"has no account {ADMIN}" in err
