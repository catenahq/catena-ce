"""Each tier group holds its tier role, and an admin's own role edits on those
groups and roles survive the converge.

The sign-in flows of realm-vps.yaml.j2 deny an account holding none of the tier
roles they admit: catena-tier-admin, held through /admin, and catena-tier-staff,
held through /staff. keycloak-config-cli makes a declared role's composites
(RealmRoleCompositeImportService) and a declared group's role mappings
(GroupImportService updateGroupRealmRoles) exact, so the realm file declares
neither and realm_bootstrap.yml adds them with kcadm after the import: it
creates a missing tier role, gives a group its tier role back, and removes
nothing. The task's script runs here against a stand-in `docker` that answers
the kcadm calls from a JSON state file and refuses any other call.

Run: uv run pytest tests/unit/test_realm_tier_roles.py
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

from test_realm_signin_flows import ADMITTED

ANSIBLE = Path(__file__).resolve().parents[2]
BOOTSTRAP = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "realm_bootstrap.yml"

CT = "keycloak_server.1.abc"
TIERS = {"/admin": "catena-tier-admin", "/staff": "catena-tier-staff"}

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
    assert opt("-r") == "vps", kc
    if kc[0] == "get" and kc[1].startswith("roles/"):
        done(0 if kc[1][len("roles/"):] in state["roles"] else 1)
    if kc[:2] == ["create", "roles"]:
        fields = dict(kc[i + 1].split("=", 1) for i, a in enumerate(kc) if a == "-s")
        state["roles"][fields["name"]] = fields["description"]
        done()
    if kc[0] == "get-roles":
        assert opt("--fields") == "name" and opt("--format") == "csv" and "--noquotes" in kc
        for name in state["groups"][opt("--gpath")]:
            print(name)
        done()
    if kc[0] == "add-roles":
        assert opt("--rolename") in state["roles"], kc
        state["groups"][opt("--gpath")].append(opt("--rolename"))
        done()
    done(f"unexpected kcadm call {kc}")
''')


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(tasks: list[dict], fragment: str) -> int:
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _task() -> dict:
    tasks = _tasks()
    return tasks[_index(tasks, "each tier group holds its tier role")]


def _script(item: dict) -> str:
    variables = {"_keycloak_realm_server_ct": {"stdout": CT}, "keycloak_realm": "vps",
                 "item": item}
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(_task()["ansible.builtin.shell"]))


def _converge(tmp_path: Path, roles: dict, groups: dict):
    """Runs the task once per loop item, as Ansible does; returns each item's
    stdout and the realm state after."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"roles": roles, "groups": groups, "calls": []}))
    outs = []
    for item in _task()["loop"]:
        proc = subprocess.run(
            ["bash", "-c", _script(item)], text=True, capture_output=True,
            env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})
        assert proc.returncode == 0, proc.stderr
        outs.append(proc.stdout)
    return outs, json.loads(state.read_text())


def _changed(stdout: str) -> bool:
    return "created:" in stdout or "granted:" in stdout


def test_a_new_realm_gets_both_tier_roles_on_their_groups(tmp_path):
    outs, state = _converge(tmp_path, roles={}, groups={"/admin": [], "/staff": []})
    assert set(state["roles"]) == set(TIERS.values())
    assert all(state["roles"].values()), "a tier role was created without a description"
    assert state["groups"] == {group: [role] for group, role in TIERS.items()}
    assert all(_changed(out) for out in outs)


def test_hand_made_mappings_stay_and_a_missing_tier_role_comes_back(tmp_path):
    roles = {"catena-tier-admin": "edited by hand", "catena-tier-staff": "x", "finance": ""}
    groups = {"/admin": ["finance", "catena-tier-admin"], "/staff": ["finance"]}
    outs, state = _converge(tmp_path, roles=roles, groups=groups)
    assert state["roles"] == roles, "an existing role was recreated or changed"
    assert state["groups"] == {"/admin": ["finance", "catena-tier-admin"],
                               "/staff": ["finance", "catena-tier-staff"]}
    assert [_changed(out) for out in outs] == [False, True]


def test_a_settled_realm_only_reads(tmp_path):
    roles = {role: "" for role in TIERS.values()}
    groups = {group: [role] for group, role in TIERS.items()}
    outs, state = _converge(tmp_path, roles=roles, groups=groups)
    assert not any(_changed(out) for out in outs)
    assert {call[0] for call in state["calls"]} == {"get", "get-roles"}


def test_the_roles_are_the_ones_the_sign_in_flows_check():
    loop = _task()["loop"]
    assert {f"/{i['group']}": i["role"] for i in loop} == TIERS
    assert {i["role"] for i in loop} == set().union(*ADMITTED.values())


def test_it_runs_on_the_kcadm_session_after_the_import_and_before_the_flow_ids():
    tasks = _tasks()
    imported = _index(tasks, "import the realm config")
    login = _index(tasks, "kcadm login to master")
    tiers = _index(tasks, "each tier group holds its tier role")
    flows = _index(tasks, "read the sign-in flow ids")
    assert imported < login < tiers < flows
    task = tasks[tiers]
    assert task["changed_when"] == (
        "'created:' in _keycloak_tier_role.stdout or 'granted:' in _keycloak_tier_role.stdout")
    assert task["register"] == "_keycloak_tier_role"
