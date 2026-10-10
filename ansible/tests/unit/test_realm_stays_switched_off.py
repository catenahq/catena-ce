"""A realm an administrator switched off stays off, and the converge completes
on it and says so; a realm Keycloak creates is switched on.

Keycloak creates a realm whose representation leaves `enabled` out switched off
(the realm row's boolean defaults to false), and keeps the flag through an
update that leaves it out (DefaultExportImportManager importRealm and
updateRealm, Keycloak 26.6). keycloak-config-cli 6.5.1 sends only the fields a
file declares (RealmImportService updateRealm, CloneUtil's non-null clone). So
only the seed that creates the realm declares `enabled: true`, and
realm_bootstrap.yml imports the seed whenever the realm is missing, on a fresh
host or over a database that lost it while the bootstrap markers remain; the
persistent file, every client file and a re-seed of an existing realm leave it
out, and no step switches the realm on.

Every later step signs in to the master realm, which a switched-off realm does
not touch; the converge reports the switch after reading the realm.

Run: uv run pytest tests/unit/test_realm_stays_switched_off.py
"""
from __future__ import annotations

import json
import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

from test_realm_bootstrap_settles import _render

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "keycloak"
BOOTSTRAP = ROLE / "tasks" / "realm_bootstrap.yml"

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

CT = "keycloak_server.1.abc"
LIST = "list the realms Keycloak holds"
SEED = "seed the admin password and the realm settings"
NOTICE = "the realm is switched off, and stays off"

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    state = json.load(open(os.environ["FAKE_KC_STATE"]))
    args = sys.argv[1:]
    assert args[:3] == ["exec", "keycloak_server.1.abc", "/opt/keycloak/bin/kcadm.sh"], args
    kc = args[3:]
    if kc[:2] == ["get", "realms"]:
        if state["down"]:
            sys.exit("HTTP request error: Connection refused")
        print("\\n".join(state["realms"]))
        sys.exit(0)
    sys.exit(f"unexpected kcadm call {kc}")
''')


def _walk(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(tasks, fragment) -> int:
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _task(fragment) -> dict:
    tasks = _tasks()
    return tasks[_index(tasks, fragment)]


def _true(expr: str, **variables) -> bool:
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template("{{ " + expr + " }}"))


# --- only the seed switches the realm on ---------------------------------------

def test_only_the_seed_that_creates_the_realm_switches_it_on():
    assert yaml.safe_load(_render(realm_seed=True, realm_create=True))["enabled"] is True
    assert "enabled" not in yaml.safe_load(_render(realm_seed=True, realm_create=False)), \
        "a re-seed over an existing realm switches it on"
    assert "enabled" not in yaml.safe_load(_render(realm_seed=False))


@pytest.mark.parametrize("realms,create", [(["master"], True), (["master", "vps"], False)])
def test_the_seed_creates_the_realm_only_when_keycloak_lacks_it(realms, create):
    seed = next(t for t in _task(SEED)["block"] if "render the realm seed" in t["name"])
    assert _true(seed["vars"]["realm_create"].strip("{} "), keycloak_realm="vps",
                 _kc_realms={"stdout_lines": realms}) is create


def test_no_client_file_declares_the_realms_switch():
    """A client file is a realm import of its own: a top-level `enabled` there
    would switch the realm on at every converge."""
    for template in sorted((ROLE / "templates").glob("realm-*.yaml.j2")):
        if template.name == "realm-vps.yaml.j2":
            continue
        assert not re.search(r"(?m)^enabled\s*:", template.read_text()), template.name


def _command(task: dict) -> str:
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(key)
        if isinstance(mod, dict) and isinstance(mod.get("argv"), list):
            return " ".join(map(str, mod["argv"]))
        if mod is not None:
            return str(mod)
    return ""


def _commands():
    for path in sorted((ROLE / "tasks").glob("*.yml")):
        for task in _walk(yaml.safe_load(path.read_text())):
            yield path.name, task, _command(task)


def test_no_step_switches_the_realm_on():
    for name, task, text in _commands():
        assert not re.search(r"""-s\s+["']?enabled=""", text), (name, task.get("name"))


# --- a missing realm is seeded -------------------------------------------------

@pytest.mark.parametrize("markers,realms,seeded", [
    ((True, True), ["master", "vps"], False),
    ((True, True), ["master"], True),
    ((False, True), ["master", "vps"], True),
    ((True, False), ["master"], True),
    ((False, False), ["master"], True),
])
def test_the_seed_runs_while_a_marker_or_the_realm_is_missing(markers, realms, seeded):
    admin, settings = markers
    assert _true(_task(SEED)["when"], keycloak_realm="vps",
                 _kc_admin_pw_marker={"stat": {"exists": admin}},
                 _kc_settings_marker={"stat": {"exists": settings}},
                 _kc_realms={"stdout_lines": realms}) is seeded


def test_the_realms_are_listed_after_the_admin_move_and_before_the_seed():
    tasks = _tasks()
    move = _index(tasks, "move the admin's accounts")
    assert move < _index(tasks, LIST) < _index(tasks, SEED)


def _list(tmp_path: Path, realms=("master", "vps"), down=False):
    """The listing, run on the session the sign-in before it opened."""
    task = _task(LIST)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"realms": list(realms), "down": down}))
    templar = Templar(loader=DataLoader(), variables={"_kc_master_login": {"stdout": CT}})
    argv = [templar.template(trust_as_template(a)) for a in task["ansible.builtin.command"]["argv"]]
    return subprocess.run(
        [str(docker), *argv[1:]], text=True, capture_output=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_KC_STATE": str(state)})


def test_the_listing_names_each_realm_on_a_line(tmp_path):
    proc = _list(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["master", "vps"]


def test_a_failed_listing_fails_rather_than_reading_as_a_missing_realm(tmp_path):
    assert _list(tmp_path, down=True).returncode != 0
    task = _task(LIST)
    assert task["ansible.builtin.command"]["argv"][0] == "docker"
    assert "failed_when" not in task


# --- the converge completes on a switched-off realm and says so ----------------

def test_every_kcadm_session_signs_in_to_the_master_realm():
    logins = [(name, re.findall(r"--realm\s+(\S+)", text))
              for name, _, text in _commands() if "config credentials" in text]
    assert len(logins) >= 2, logins
    for name, realms in logins:
        assert realms and set(realms) == {"master"}, name


@pytest.mark.parametrize("enabled,shown", [(False, True), (True, False)])
def test_a_switched_off_realm_is_reported(enabled, shown):
    task = _task(NOTICE)
    realm = {"realm": "vps", "enabled": enabled}
    assert _true(task["when"], _keycloak_realm_rep={"stdout": json.dumps(realm)}) is shown
    assert "leaves it off" in task["ansible.builtin.debug"]["msg"]


def test_the_report_follows_the_realm_read_and_precedes_the_flow_ids():
    tasks = _tasks()
    order = [_index(tasks, f) for f in ("read the realm", NOTICE, "read the sign-in flow ids")]
    assert order == sorted(order)
