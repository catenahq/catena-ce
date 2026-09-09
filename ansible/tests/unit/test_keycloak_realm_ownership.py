"""The converge writes the door, never who walks through it.

The catena-admin panel manages this realm's users, groups and memberships. By
the ownership test that governs this workspace those are CLIENT-owned data -- a
user list changes when the client hires somebody, not when the product version
changes -- so the converge must never reconcile them, and the panel is their
single writer.

What the converge DOES own is the capability: the service-account client, its
secret, and the realm-management roles it may use. That distinction is the whole
of this file.

A realm template that declared `users:` for a real person would make the
converge a second writer of the client's directory, and the failure mode is the
worst kind: every converge would silently reset whatever the panel had done to
whatever the template said, and the operator would watch their change disappear
on a schedule nobody connects to it.

Run: uv run pytest tests/unit/test_keycloak_realm_ownership.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TEMPLATES = ANSIBLE / "reconcile" / "roles" / "keycloak" / "templates"
PANEL = TEMPLATES / "realm-admin-panel.yaml.j2"
PROBE = TEMPLATES / "realm-identity-probe.yaml.j2"

# The identity probe supervises this realm and must never be able to change it.
# A supervision credential that could write could hide a drift by correcting
# it, and the report would then describe a realm the probe made rather than the
# one the client has.
PROBE_EXPECTED_ROLES = {
    "view-users", "query-users", "query-groups",
    "view-realm", "view-identity-providers", "view-clients",
}

# The three roles the panel needs and the reason each is the smallest that will
# do. Pinned so widening the grant is a deliberate edit to a test, not a quiet
# addition to a YAML list.
EXPECTED_ROLES = {"manage-users", "view-users", "query-groups"}

# Roles that would let this credential change what an APPLICATION trusts, as
# opposed to who may use it. A leak that can add a person is the panel's job
# working as designed; a leak that can rewrite an OIDC registration is a way to
# take over a login.
FORBIDDEN_ROLES = {
    "manage-clients", "manage-realm", "manage-authorization",
    "manage-identity-providers", "realm-admin", "view-realm",
}


def _render(text: str) -> str:
    """Strip the Jinja so the YAML parses. Values do not matter here: what is
    under test is the SHAPE of what the converge declares."""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if not stripped.strip():
            continue
        out.append(line.replace("{{ ansible_managed }}", "managed")
                   .replace("{{ keycloak_realm }}", "vps")
                   .replace("{{ catena_admin_panel_client_id }}", "catena-admin-panel")
                   .replace("{{ catena_admin_panel_client_secret }}", "secret")
                   .replace("{{ catena_identity_probe_client_id }}", "catena-identity-probe")
                   .replace("{{ catena_identity_probe_client_secret }}", "secret"))
    return "\n".join(out)


def _doc(path: Path) -> dict:
    return yaml.safe_load(_render(path.read_text())) or {}


def test_the_panel_client_grants_exactly_the_three_roles():
    doc = _doc(PANEL)
    users = doc.get("users") or []
    assert len(users) == 1, f"expected one service-account user, got {users}"
    roles = set((users[0].get("clientRoles") or {}).get("realm-management") or [])
    assert roles == EXPECTED_ROLES, (
        f"realm-management roles = {sorted(roles)}; widening this grant is a "
        f"deliberate decision, not a YAML edit"
    )


def test_the_panel_client_cannot_change_what_an_application_trusts():
    doc = _doc(PANEL)
    roles = set()
    for u in doc.get("users") or []:
        roles |= set((u.get("clientRoles") or {}).get("realm-management") or [])
    overreach = roles & FORBIDDEN_ROLES
    assert not overreach, (
        f"the People panel's credential holds {sorted(overreach)}. It manages "
        f"PEOPLE; a credential that can also rewrite a client's OIDC "
        f"registration is a way to take over a login, not to manage access."
    )


def test_the_identity_probe_can_only_read():
    """The probe reports whether this realm still matches the managed-identity
    promise. Any manage-* role would let the thing doing the reporting change
    what it reports on."""
    doc = _doc(PROBE)
    users = doc.get("users") or []
    assert len(users) == 1, f"expected one service-account user, got {users}"
    roles = set((users[0].get("clientRoles") or {}).get("realm-management") or [])
    assert roles == PROBE_EXPECTED_ROLES, (
        f"realm-management roles = {sorted(roles)}; the probe's grant is "
        f"read-only by design"
    )
    writers = {r for r in roles if r.startswith("manage-") or r == "realm-admin"}
    assert not writers, (
        f"the identity probe holds {sorted(writers)}. A supervision credential "
        f"that can write can hide a drift by correcting it."
    )


def test_the_probe_and_the_panel_do_not_share_a_credential():
    """Each holds something the other is deliberately denied: the panel has
    manage-users, which a supervision credential must not; the probe has
    view-realm, which the panel is refused because it exposes the realm's whole
    configuration to a credential that only needs to add people. One shared
    client would hand each side the other's reach."""
    panel = _doc(PANEL)["clients"][0]["clientId"]
    probe = _doc(PROBE)["clients"][0]["clientId"]
    assert panel != probe, f"both service accounts are {panel!r}"


def test_the_only_users_any_realm_template_declares_are_the_install_itself():
    """A realm template naming a real person makes the converge a second writer
    of the client's directory, and every converge would silently reset whatever
    the panel had done -- the operator watching their change disappear on a
    schedule nobody connects to it.

    Two exceptions, both the INSTALL rather than the client's directory: the
    service accounts the converge owns outright, and the first administrator,
    whose address is an install input. Everybody hired afterwards is the
    panel's.

    Scanned as TEXT, not parsed. Two of these templates carry Jinja control
    flow and do not parse as YAML, and skipping them would leave the blind spot
    exactly where a loop could generate users."""
    allowed = ("service-account-", "{{ admin_email }}")
    for template in sorted(TEMPLATES.glob("realm-*.yaml.j2")):
        for i, line in enumerate(template.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "username:" not in code:
                continue
            assert any(a in code for a in allowed), (
                f"{template.name}:{i} declares a user that is neither a "
                f"service account nor the install's own administrator: "
                f"{code.strip()!r}. The converge owns the install; the people "
                f"hired afterwards are the panel's, and a converge that writes "
                f"them resets the client's directory on every run."
            )


# The four tiers the PRODUCT itself knows. shell/labels.ResolveAuthMode
# defaults a private app to {admin, client, staff} and treats `visitor` as the
# keyword meaning public, so these names are product shape and the converge is
# right to seed them. Anything else is a group the client made up, which is
# theirs.
PRODUCT_TIERS = {"admin", "staff", "client", "visitor"}


def test_realm_templates_seed_the_product_tiers_and_nothing_else():
    """The converge seeds the four tiers the product's own auth resolution
    names, because an app labelled vps.auth.groups=staff has to resolve against
    something on a realm nobody has touched yet. A FIFTH group in a template
    would be the converge inventing a client's org chart -- and recreating it
    on every run after the panel deleted it."""
    for template in sorted(TEMPLATES.glob("realm-*.yaml.j2")):
        lines = template.read_text().splitlines()
        inside = False
        for i, line in enumerate(lines, 1):
            code = line.split("#", 1)[0].rstrip()
            if code == "groups:":
                inside = True
                continue
            if inside and code and not code.startswith(" "):
                inside = False
            if not inside or "- name:" not in code:
                continue
            name = code.split("- name:", 1)[1].strip().strip('"').strip("'")
            assert name in PRODUCT_TIERS, (
                f"{template.name}:{i} seeds realm group {name!r}, which is not "
                f"one of the product tiers {sorted(PRODUCT_TIERS)}. Groups "
                f"beyond those are the client's, and the panel is their single "
                f"writer -- a converge that recreated one would restore access "
                f"somebody deliberately removed."
            )


def test_subgroups_are_left_to_the_client():
    """Departments under /staff are the client's org chart. The template says
    so in a comment and keeps them commented out; uncommenting one would make
    every converge recreate a department the panel had deleted."""
    for template in sorted(TEMPLATES.glob("realm-*.yaml.j2")):
        for i, line in enumerate(template.read_text().splitlines(), 1):
            code = line.split("#", 1)[0].rstrip()
            assert "subGroups:" not in code, (
                f"{template.name}:{i} declares subGroups. Departments are the "
                f"client's org chart, not the product's."
            )


def test_the_two_service_accounts_hold_different_roles():
    """dashboard-sync manages CLIENTS; the panel manages PEOPLE. One credential
    holding both would mean a leak intended to add a user could rewrite what an
    application trusts instead."""
    panel = _doc(PANEL)
    sync = _doc(TEMPLATES / "realm-dashboard-sync.yaml.j2")

    def roles(doc):
        out = set()
        for u in doc.get("users") or []:
            out |= set((u.get("clientRoles") or {}).get("realm-management") or [])
        return out

    assert not (roles(panel) & roles(sync)), (
        "the two service accounts share a realm-management role; the split is "
        "the point of having two"
    )


def test_the_client_is_confidential_and_has_no_interactive_flow():
    """A service account authenticates with client_credentials and nothing
    else. A public client, or one with the direct-access grant, would be a
    credential a browser could be talked into using."""
    doc = _doc(PANEL)
    clients = doc.get("clients") or []
    assert len(clients) == 1
    c = clients[0]
    assert c.get("publicClient") is False
    assert c.get("serviceAccountsEnabled") is True
    for flow in ("standardFlowEnabled", "implicitFlowEnabled",
                 "directAccessGrantsEnabled"):
        assert c.get(flow) is False, f"{flow} is enabled on a service account"
