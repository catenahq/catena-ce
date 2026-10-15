"""The realm's mail settings follow Settings > Outgoing mail while they are the
ones Catena wrote, and settings changed in the realm's Email tab stay.

The realm records a fingerprint of the mail fields it was last given in a realm
attribute. Before the imports, realm_bootstrap.yml reads the realm and
keycloak_mail_follows (playbooks/filter_plugins/keycloak_mail_settings.py)
decides whether the realm files declare `smtpServer`: a realm with no mail
settings or no record takes them (a realm without a record is adopted, whatever
it holds); one with a record takes them while its settings match the record.
After the imports, keycloak_mail_record_sets records the
fingerprint once the realm holds what Catena would write.

Run: uv run pytest tests/unit/test_realm_mail_settings.py
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

from test_realm_bootstrap_settles import DEFAULTS, _smtp_server

ANSIBLE = Path(__file__).resolve().parents[2]
BOOTSTRAP = ANSIBLE / "reconcile" / "roles" / "keycloak" / "tasks" / "realm_bootstrap.yml"

sys.path.insert(0, str(ANSIBLE / "playbooks" / "filter_plugins"))
from keycloak_mail_settings import (  # noqa: E402
    keycloak_mail_fingerprint, keycloak_mail_follows, keycloak_mail_record_sets)

add_all_plugin_dirs(str(ANSIBLE / "playbooks"))

ATTR = yaml.safe_load(DEFAULTS.read_text())["keycloak_realm_mail_attribute"]
CT = "keycloak_server.1.abc"
WANTED = _smtp_server()
OTHER = {**WANTED, "host": "smtp.brevo.com", "port": "465"}
HAND = {"host": "mail.acme.test", "port": "25", "from": "ops@acme.test", "auth": "false"}
FOLLOW = "decide whether the imports write the mail settings"
RECORD = "record the mail settings Catena wrote"


def _realm(smtp=None, record=None, **attributes) -> dict:
    """A realm as `kcadm get realms/vps` prints it: the stored password comes
    back masked."""
    attrs = dict(attributes)
    if record is not None:
        attrs[ATTR] = record
    realm = {"realm": "vps", "enabled": True, "attributes": attrs}
    if smtp is not None:
        realm["smtpServer"] = {**smtp, "password": "**********"} if smtp else {}
    return realm


def _print(smtp) -> str:
    return keycloak_mail_fingerprint(smtp)


def _written(sets: list[str]) -> dict:
    assert sets[::2] == ["-s"] * (len(sets) // 2), sets
    return {k: json.loads(v) for k, v in (s.split("=", 1) for s in sets[1::2])}


# --- before the imports ---------------------------------------------------------

@pytest.mark.parametrize("realm", [None, {}, _realm(), _realm({}), _realm({}, record="x")],
                         ids=["no-realm", "empty", "no-settings", "empty-map", "cleared"])
def test_a_realm_with_no_mail_settings_takes_them(realm):
    assert keycloak_mail_follows(realm, ATTR) is True


@pytest.mark.parametrize("smtp", [WANTED, HAND, OTHER], ids=["equal", "hand", "stale"])
def test_a_realm_with_no_record_is_adopted_whatever_it_holds(smtp):
    assert keycloak_mail_follows(_realm(smtp), ATTR) is True


def test_settings_matching_the_record_follow_a_panel_change():
    assert keycloak_mail_follows(_realm(OTHER, record=_print(OTHER)), ATTR) is True


def test_settings_differing_from_the_record_are_a_hand_edit_and_stay():
    assert keycloak_mail_follows(_realm(HAND, record=_print(WANTED)), ATTR) is False


def test_a_hand_edit_equal_to_the_panel_but_not_the_record_stays():
    """Different from the record is a hand edit, though it equals what the panel
    says now: nothing is declared, and the step after the imports records it."""
    assert keycloak_mail_follows(_realm(WANTED, record=_print(OTHER)), ATTR) is False


def test_the_password_is_not_compared():
    realm = _realm(WANTED, record=_print(WANTED))
    realm["smtpServer"]["password"] = "something-else"
    assert keycloak_mail_follows(realm, ATTR) is True


def test_only_the_fingerprinted_fields_count():
    realm = _realm({**WANTED, "authType": "basic", "debug": "true"}, record=_print(WANTED))
    assert keycloak_mail_follows(realm, ATTR) is True


@pytest.mark.parametrize("field,value", [
    ("host", "mail.acme.test"), ("port", "25"), ("from", "x@acme.test"),
    ("fromDisplayName", "Acme"), ("replyTo", "reply@acme.test"), ("user", "someone"),
    ("auth", "false"), ("ssl", "true"), ("starttls", "false"),
])
def test_a_change_to_any_compared_field_is_a_hand_edit(field, value):
    realm = _realm({**WANTED, field: value}, record=_print(WANTED))
    assert keycloak_mail_follows(realm, ATTR) is False


def test_flags_compare_without_case():
    realm = _realm({**WANTED, "starttls": "TRUE"}, record=_print(WANTED))
    assert keycloak_mail_follows(realm, ATTR) is True


# --- after the imports -----------------------------------------------------------

def test_the_record_is_written_once_the_realm_holds_what_catena_writes():
    sets = keycloak_mail_record_sets(_realm(WANTED, record=_print(OTHER)), WANTED, ATTR)
    assert _written(sets) == {f"attributes.{ATTR}": _print(WANTED)}
    assert isinstance(_written(sets)[f"attributes.{ATTR}"], str)


def test_a_realm_without_a_record_gets_it_when_its_settings_equal_catenas():
    assert _written(keycloak_mail_record_sets(_realm(WANTED), WANTED, ATTR)) == \
        {f"attributes.{ATTR}": _print(WANTED)}


def test_a_realm_with_mail_off_records_the_empty_settings():
    assert _written(keycloak_mail_record_sets(_realm({}), {}, ATTR)) == \
        {f"attributes.{ATTR}": _print({})}


def test_the_next_converge_records_nothing():
    assert keycloak_mail_record_sets(_realm(WANTED, record=_print(WANTED)), WANTED, ATTR) == []


def test_a_hand_edit_is_not_recorded():
    assert keycloak_mail_record_sets(_realm(HAND, record=_print(WANTED)), WANTED, ATTR) == []
    assert keycloak_mail_record_sets(_realm(HAND), WANTED, ATTR) == []


def test_a_new_domain_changes_the_fingerprint_with_the_sender_name():
    assert _print(_smtp_server(display_name="acme-group.test")) != _print(WANTED)


# --- the converge's steps --------------------------------------------------------

def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(BOOTSTRAP.read_text()) if isinstance(t, dict)]


def _index(tasks, fragment) -> int:
    return next(i for i, t in enumerate(tasks) if fragment in str(t.get("name", "")))


def _variables(**extra) -> dict:
    return {"keycloak_smtp_server": WANTED, "keycloak_realm_mail_attribute": ATTR,
            "keycloak_realm": "vps", "_keycloak_realm_server_ct": {"stdout": CT}, **extra}


def _decision(realm: dict | None) -> bool:
    """keycloak_smtp_write as the set_fact step renders it for one realm read, or
    for a realm that was not read (the read step skipped)."""
    fact = _tasks()[_index(_tasks(), FOLLOW)]["ansible.builtin.set_fact"]["keycloak_smtp_write"]
    before = {"skipped": True} if realm is None else {"stdout": json.dumps(realm)}
    templar = Templar(loader=DataLoader(), variables=_variables(_kc_realm_before=before))
    return str(templar.template(trust_as_template(fact))) == "True"


def test_the_decision_reads_the_realm_it_was_given():
    assert _decision(None) is True
    assert _decision(_realm(WANTED, record=_print(WANTED))) is True
    assert _decision(_realm(HAND, record=_print(WANTED))) is False


def test_the_record_step_updates_the_realm_it_read():
    task = _tasks()[_index(_tasks(), RECORD)]
    variables = _variables(_keycloak_realm_rep={"stdout": json.dumps(_realm(WANTED))})
    templar = Templar(loader=DataLoader(), variables=variables)
    variables["_keycloak_mail_record_sets"] = templar.template(
        trust_as_template(task["vars"]["_keycloak_mail_record_sets"]))
    templar = Templar(loader=DataLoader(), variables=variables)
    argv = templar.template(trust_as_template(task["ansible.builtin.command"]["argv"]))
    assert templar.template(trust_as_template("{{ " + task["when"] + " }}")) is True
    assert argv[:6] == ["docker", "exec", CT, "/opt/keycloak/bin/kcadm.sh", "update", "realms/vps"]
    assert "-n" not in argv and "-f" not in argv, "kcadm merges only without a body"
    assert _written(argv[6:]) == {f"attributes.{ATTR}": _print(WANTED)}


def test_the_realm_is_read_before_either_render_and_the_record_follows_the_import():
    tasks = _tasks()
    read = tasks[_index(tasks, "read the mail settings before the imports")]
    argv = read["ansible.builtin.command"]["argv"]
    assert argv[3:] == ["/opt/keycloak/bin/kcadm.sh", "get", "realms/{{ keycloak_realm }}"], \
        "--fields prints the attributes and smtpServer maps with their values dropped"
    assert read["when"] == "keycloak_realm in _kc_realms.stdout_lines"
    order = [_index(tasks, f) for f in (
        "list the realms Keycloak holds", "read the mail settings before the imports", FOLLOW,
        "seed the admin password and the realm settings", "render realm config", "import the realm config",
        "read the realm", "the sign-in page name follows the domain", RECORD)]
    assert order == sorted(order)


def test_the_converge_says_when_it_leaves_the_mail_settings():
    task = _tasks()[_index(_tasks(), "mail settings were changed in Keycloak")]
    assert task["when"] == "not (keycloak_smtp_write | bool)"
    assert "Email" in task["ansible.builtin.debug"]["msg"]
