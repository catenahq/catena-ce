"""The sign-in page name follows the domain while it is the name Catena wrote,
and a name a person gave the page stays.

The realm seed writes the page's two forms, displayName and displayNameHtml,
from the domain, and records the name in a realm attribute beside them. After
the import, realm_bootstrap.yml reads the whole realm and
keycloak_signin_name_sets (playbooks/filter_plugins/keycloak_signin_name.py)
decides: both forms equal to the record and another domain move both forms and
the record; a form that differs is a person's name, and the page keeps it. A
realm seeded before the record counts its name as Catena's when it is the
seed's for the current domain, or the seed's empty one from a host with no
domain yet, and records it. kcadm merges the changes into the realm it reads.

Run: uv run pytest tests/unit/test_realm_signin_name.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import add_all_plugin_dirs
from ansible.template import Templar, trust_as_template

from test_realm_bootstrap_settles import DEFAULTS, _render

ANSIBLE = Path(__file__).resolve().parents[2]
BOOTSTRAP = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "realm_bootstrap.yml"

sys.path.insert(0, str(ANSIBLE / "playbooks" / "filter_plugins"))
from keycloak_signin_name import keycloak_signin_name_sets  # noqa: E402

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

ATTR = yaml.safe_load(DEFAULTS.read_text())["keycloak_realm_display_name_attribute"]
CT = "keycloak_server.1.abc"
OLD, NEW = "acme.test", "acme-group.test"
READ = "read the realm"
FOLLOW = "the sign-in page name follows the domain"


def _realm(name=None, html=None, record=None, **attributes) -> dict:
    """A realm as `kcadm get realms/vps` prints it, cut to what is read."""
    attrs = dict(attributes)
    if record is not None:
        attrs[ATTR] = record
    realm = {"realm": "vps", "enabled": True, "attributes": attrs}
    if name is not None:
        realm["displayName"] = name
    if html is not None:
        realm["displayNameHtml"] = html
    return realm


def _catenas(name, record=None) -> dict:
    return _realm(name, f"<b>{name}</b>", record)


def _written(sets: list[str]) -> dict:
    """{field: value} of kcadm `-s field=value` arguments, each value read the
    way kcadm reads it: as JSON when it parses."""
    assert sets[::2] == ["-s"] * (len(sets) // 2), sets
    return {k: json.loads(v) for k, v in (s.split("=", 1) for s in sets[1::2])}


def _moved_to(name) -> dict:
    return {"displayName": name, "displayNameHtml": f"<b>{name}</b>", f"attributes.{ATTR}": name}


# --- the seed ------------------------------------------------------------------

def test_the_seed_writes_both_forms_and_records_the_name():
    seed = yaml.safe_load(_render(realm_seed=True))
    assert seed["displayName"] == "acme.test"
    assert seed["displayNameHtml"] == "<b>acme.test</b>"
    assert seed["attributes"][ATTR] == "acme.test"


def test_a_freshly_seeded_realm_is_settled():
    assert keycloak_signin_name_sets(yaml.safe_load(_render(realm_seed=True)), "acme.test",
                                     ATTR) == []


def test_the_persistent_file_writes_neither_form_nor_the_record():
    persistent = yaml.safe_load(_render(realm_seed=False))
    assert "displayName" not in persistent and "displayNameHtml" not in persistent
    assert "attributes" not in persistent, "a declared attribute map is made exact"


# --- the rule --------------------------------------------------------------------

def test_a_new_domain_moves_both_forms_and_the_record():
    assert _written(keycloak_signin_name_sets(_catenas(OLD, record=OLD), NEW, ATTR)) == \
        _moved_to(NEW)


def test_the_next_converge_leaves_it():
    assert keycloak_signin_name_sets(_catenas(NEW, record=NEW), NEW, ATTR) == []


@pytest.mark.parametrize("realm", [
    _realm("Acme Inc", "<b>acme.test</b>", OLD),
    _realm(OLD, "<img src=logo.png> Acme", OLD),
    _realm("Acme Inc", "Acme Inc", OLD),
    _realm(None, None, OLD),
], ids=["plain-name", "page-header", "both", "cleared"])
def test_a_name_a_person_gave_the_page_stays(realm):
    assert keycloak_signin_name_sets(realm, NEW, ATTR) == []


def test_a_realm_restored_from_another_domain_follows_this_one():
    """The record is in the realm, so it comes back with the database."""
    restored = _catenas("source.test", record="source.test")
    assert _written(keycloak_signin_name_sets(restored, NEW, ATTR)) == _moved_to(NEW)


def test_other_attributes_are_neither_read_nor_written():
    sets = keycloak_signin_name_sets(_realm(OLD, f"<b>{OLD}</b>", OLD, frontendUrl="x"), NEW, ATTR)
    assert set(_written(sets)) == set(_moved_to(NEW))


# --- a realm seeded before the record --------------------------------------------

def test_a_seed_name_for_this_domain_is_recorded():
    assert _written(keycloak_signin_name_sets(_catenas(OLD), OLD, ATTR)) == _moved_to(OLD)


def test_the_empty_seed_name_of_a_host_with_no_domain_yet_follows_the_domain():
    for realm in (_realm("", "<b></b>"), _realm(None, "<b></b>")):
        assert _written(keycloak_signin_name_sets(realm, NEW, ATTR)) == _moved_to(NEW)


@pytest.mark.parametrize("realm", [
    _catenas("old-domain.test"),
    _realm("Acme Inc", "<b>Acme Inc</b>"),
    _realm(OLD, "Acme"),
    _realm("", ""),
], ids=["another-domain", "trading-name", "one-form", "cleared"])
def test_any_other_name_on_such_a_realm_is_a_persons(realm):
    assert keycloak_signin_name_sets(realm, OLD, ATTR) == []


# --- no domain, and kcadm's reading of values -----------------------------------

@pytest.mark.parametrize("name", ["", "  ", None])
def test_with_no_domain_the_page_keeps_its_name(name):
    assert keycloak_signin_name_sets(_catenas(OLD, record=OLD), name, ATTR) == []


@pytest.mark.parametrize("name", ["true.example", "null.example", "123.example",
                                  "xn--bcher-kva.example"])
def test_every_value_reaches_keycloak_as_the_text_it_holds(name):
    written = _written(keycloak_signin_name_sets(_catenas(OLD, record=OLD), name, ATTR))
    assert written == _moved_to(name)
    assert all(isinstance(v, str) for v in written.values())


# --- the converge's steps ---------------------------------------------------------

def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(tasks, fragment) -> int:
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _follow(realm: dict, name: str) -> tuple[list, bool]:
    """The update step's argv and `when`, rendered the way Ansible renders them
    for one realm read and one domain."""
    task = _tasks()[_index(_tasks(), FOLLOW)]
    variables = {
        "_keycloak_realm_server_ct": {"stdout": CT}, "keycloak_realm": "vps",
        "_keycloak_realm_rep": {"stdout": json.dumps(realm)},
        "keycloak_realm_display_name": name, "keycloak_realm_display_name_attribute": ATTR,
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    variables["_keycloak_signin_name_sets"] = templar.template(
        trust_as_template(task["vars"]["_keycloak_signin_name_sets"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    argv = templar.template(trust_as_template(task["ansible.builtin.command"]["argv"]))
    run = templar.template(trust_as_template("{{ " + task["when"] + " }}"))
    return argv, run


def test_the_step_updates_the_realm_it_read():
    argv, run = _follow(_catenas(OLD, record=OLD), NEW)
    assert run is True
    assert argv[:6] == ["docker", "exec", CT, "/opt/keycloak/bin/kcadm.sh", "update", "realms/vps"]
    assert "-n" not in argv and "-f" not in argv, "kcadm merges only without a body"
    assert _written(argv[6:]) == _moved_to(NEW)


def test_the_step_does_not_run_on_a_name_a_person_gave():
    _, run = _follow(_realm("Acme Inc", "<b>Acme Inc</b>", OLD), NEW)
    assert run is False


def test_the_realm_is_read_whole_after_the_import_and_before_the_flow_ids():
    tasks = _tasks()
    argv = tasks[_index(tasks, READ)]["ansible.builtin.command"]["argv"]
    assert argv[3:] == ["/opt/keycloak/bin/kcadm.sh", "get", "realms/{{ keycloak_realm }}"], \
        "--fields prints the attributes map with its values dropped"
    order = [_index(tasks, f) for f in ("import the realm config", "kcadm login to master",
                                        READ, FOLLOW, "read the sign-in flow ids")]
    assert order == sorted(order)
