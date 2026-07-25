"""Lock the journal-seal check as a COMMUNITY action.

Forward Secure Sealing is set up by the `common` role on EVERY host, so the
ability to CHECK the seal cannot be a paid feature: a Community host whose
journal was tampered with must be able to find that out. What the licence gates
is the audit PANEL that renders the trail, not the tamper evidence underneath
it.

The action also has to be argument-free. The verification key is deliberately
absent from the server (whoever holds it can re-seal an edited journal), so
this dispatch verifies structure and tells the reader how to check the seals
with the key they hold off-box.

Run: uv run pytest tests/unit/test_catena_admin_fss_action.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"

FSS_ACTION = "catena-audit-fss-verify"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _ce_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ce_reserved_actions"]}


def _ee_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ee_reserved_actions"]}


def test_seal_check_is_community():
    assert FSS_ACTION in _ce_actions()
    assert FSS_ACTION not in _ee_actions(), (
        "the seal is set up on every host by roles/common; gating the CHECK "
        "would leave a Community host unable to detect tampering it is "
        "already protected against"
    )


def test_seal_check_takes_no_key_argument():
    shell = _ce_actions()[FSS_ACTION]
    assert shell.endswith("fss-verify"), shell
    assert "verify-key" not in shell, (
        "the verification key must not be passed on the box: holding it is "
        "what lets an attacker re-seal an altered journal"
    )


def test_audit_binary_path_is_shared_by_both_callers():
    # Defaults are read raw, so these are the Jinja references as written --
    # which is the point: the Business export and the Community seal check must
    # resolve the SAME variable. A literal path in one of them is how the two
    # drift when the payload moves.
    assert _defaults()["catena_admin_audit_bin"].startswith("/usr/local/bin/")
    assert _ee_actions()["audit-export"] == "{{ catena_admin_audit_bin }} export"
    assert _ce_actions()[FSS_ACTION] == "{{ catena_admin_audit_bin }} fss-verify"
