"""The port reconciler must record what it APPLIED, not what it planned.

`apply_docker_user` returned early when the DOCKER-USER chain was absent --
and `reconcile()` then called `save_applied(plan)` and `write_effective(...)`
and the unit exited 0. So a host on which every restricted-port DROP guard
was missing produced byte-identical artifacts to a correctly guarded one, and
`validate.yml` read those artifacts and passed.

That is the worst shape of the silent-skip class: not "a lane did nothing"
but "a security control did nothing and said it had". Docker's DNAT bypasses
ufw INPUT entirely, so with no DOCKER-USER rule a restricted port is simply
open to whatever can route to it.

What must hold:
  - a rule that failed to install is not in the applied state;
  - the summary carries the count that did not install, because every other
    field in it describes what is DECLARED;
  - the process exits non-zero, so systemd records the failure.

Run: uv run pytest tests/unit/test_public_ports_records_reality.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE_DIR / "scripts" / "catena-public-ports.py"
HELPERS = ANSIBLE_DIR / "helpers"

_FRAGMENT = [
    {"proto": "tcp", "port": 9000, "scope": "tailnet", "bind": "docker",
     "owner": "portainer", "comment": "control plane UI"},
]


@pytest.fixture
def mod(tmp_path, monkeypatch):
    """Load the reconciler with every artifact path redirected into tmp.

    On a host the script sits flat beside public_ports.py; in the repo they
    live in scripts/ and helpers/, so helpers/ goes on the path first.
    """
    monkeypatch.setenv("CATENA_PORTS_FRAGMENT_DIR", str(tmp_path / "d"))
    for var, name in (
        ("CATENA_PORTS_EFFECTIVE_JSON", "effective.json"),
        ("CATENA_PORTS_EFFECTIVE_DOC", "effective.md"),
        ("CATENA_PORTS_EFFECTIVE_SUMMARY", "summary.json"),
        ("CATENA_PORTS_APPLIED_STATE", "applied.json"),
    ):
        monkeypatch.setenv(var, str(tmp_path / name))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "portainer.json").write_text(json.dumps(_FRAGMENT))

    monkeypatch.syspath_prepend(str(HELPERS))
    spec = importlib.util.spec_from_file_location("catena_public_ports", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["catena_public_ports"] = m
    spec.loader.exec_module(m)
    # No live containers to harvest; the fragment feeder is the whole input.
    monkeypatch.setattr(m, "harvest_label_entries", lambda: [])
    return m


def _summary(mod) -> dict:
    return json.loads(Path(mod.EFFECTIVE_SUMMARY).read_text())


def _applied(mod) -> list:
    return json.loads(Path(mod.APPLIED_STATE).read_text())


def test_the_fixture_exercises_both_engines(mod, monkeypatch):
    """A tailnet-scoped docker-bound port plans BOTH ufw allows and
    DOCKER-USER RETURN/DROP guards. Without this the two failure tests below
    could pass vacuously against a plan that had no rules of that engine."""
    monkeypatch.setattr(mod, "_docker_user_chain_present", lambda: True)
    monkeypatch.setattr(mod, "_run", lambda argv, check_only=False: 0)
    mod.reconcile()
    engines = {r["engine"] for r in _applied(mod)}
    assert engines == {"ufw", "docker-user"}


def test_an_absent_docker_user_chain_is_not_recorded_as_applied(mod, monkeypatch):
    monkeypatch.setattr(mod, "_docker_user_chain_present", lambda: False)
    monkeypatch.setattr(mod, "_run", lambda argv, check_only=False: 0)

    _entries, unapplied = mod.reconcile()

    assert unapplied > 0
    assert not any(r["engine"] == "docker-user" for r in _applied(mod))
    assert _summary(mod)["rules_unapplied"] == unapplied


def test_a_failed_ufw_add_is_not_recorded_as_applied(mod, monkeypatch):
    monkeypatch.setattr(mod, "_docker_user_chain_present", lambda: True)
    # ufw fails, iptables succeeds.
    monkeypatch.setattr(
        mod, "_run",
        lambda argv, check_only=False: 1 if argv[0] == "ufw" else 0,
    )

    _entries, unapplied = mod.reconcile()

    assert not any(r["engine"] == "ufw" for r in _applied(mod))
    assert _summary(mod)["rules_unapplied"] == unapplied


def test_a_clean_run_records_everything_and_reports_zero(mod, monkeypatch):
    monkeypatch.setattr(mod, "_docker_user_chain_present", lambda: True)
    monkeypatch.setattr(mod, "_run", lambda argv, check_only=False: 0)

    _entries, unapplied = mod.reconcile()

    assert unapplied == 0
    assert _summary(mod)["rules_unapplied"] == 0
    assert _applied(mod), "a clean run must record the rules it installed"


def test_an_already_present_docker_user_rule_counts_as_applied(mod, monkeypatch):
    """`iptables -C` returning 0 means the guard is already there. Treating
    only fresh appends as applied would report a steady-state host as
    unguarded on every single reconcile."""
    monkeypatch.setattr(mod, "_docker_user_chain_present", lambda: True)
    monkeypatch.setattr(mod, "_run", lambda argv, check_only=False: 0)

    _entries, unapplied = mod.reconcile()

    assert unapplied == 0
    assert any(r["engine"] == "docker-user" for r in _applied(mod))


def test_the_summary_carries_the_field_validate_asserts_on(mod, monkeypatch):
    """validate.yml asserts rules_unapplied == 0. A summary written without
    the key would make that assertion vacuous via its own default."""
    monkeypatch.setattr(mod, "_docker_user_chain_present", lambda: True)
    monkeypatch.setattr(mod, "_run", lambda argv, check_only=False: 0)
    mod.reconcile()
    assert "rules_unapplied" in _summary(mod)


def test_validate_asserts_the_field():
    body = (ANSIBLE_DIR / "playbooks" / "validate.yml").read_text()
    assert "rules_unapplied" in body, (
        "the count is only useful if something reads it; validate.yml is "
        "where a scan of an unguarded host would otherwise read clean"
    )
