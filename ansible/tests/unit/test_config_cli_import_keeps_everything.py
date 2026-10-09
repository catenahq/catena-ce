"""The realm import deletes nothing: every managed-entity type of the pinned
keycloak-config-cli release is set to NO_DELETE, and a declared user keeps the
groups and realm roles given to it by hand.

keycloak-config-cli defaults most `import.managed.*` types to FULL, which
deletes every entity of that type a file does not declare, and each per-app
realm file declares only its own. MANAGED is the field list of the release
keycloak_config_cli_image_tag pins (ImportConfigProperties.ImportManagedProperties
in src/main/java/de/adorsys/keycloak/config/properties/ImportConfigProperties.java;
its application.properties leaves out sub-group and organization). sub-group
FULL deletes every department under /staff whenever the import updates /staff.
A tag bump fails test_the_list_is_the_pinned_releases until MANAGED is read
again from the new release.

Without import.users.merge-groups and merge-roles the import removes every
group and realm role a declared user holds beyond its file's list
(UserImportService handleGroups and handleRealmRoles), so the realm admin would
lose each department or role given to it by hand on every converge.

Run: uv run pytest tests/unit/test_config_cli_import_keeps_everything.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "keycloak"
IMPORT = ROLE / "tasks" / "_config_cli_import.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

RELEASE = "6.5.1"
MANAGED = (
    "required-action", "group", "sub-group", "client-scope", "scope-mapping",
    "client-scope-mapping", "component", "sub-component", "authentication-flow",
    "identity-provider", "identity-provider-mapper", "role", "client",
    "client-authorization-resources", "client-authorization-policies",
    "client-authorization-scopes", "message-bundles", "organization", "workflow",
)


def _argv() -> list[str]:
    task = yaml.safe_load(IMPORT.read_text())[0]
    return [str(a) for a in task["ansible.builtin.command"]["argv"]]


def test_the_list_is_the_pinned_releases():
    tag = yaml.safe_load(DEFAULTS.read_text())["keycloak_config_cli_image_tag"]
    assert tag.split("-", 1)[0] == RELEASE, (
        f"keycloak-config-cli moved to {tag}: read its import.managed.* "
        "properties into MANAGED and the import's argv")


def test_every_managed_type_is_no_delete():
    argv = _argv()
    for prop in MANAGED:
        env = "IMPORT_MANAGED_" + prop.upper().replace("-", "_")
        assert f"{env}=NO_DELETE" in argv, f"{env} is left at the release default"


def test_no_managed_type_is_set_otherwise():
    managed = [a for a in _argv() if a.startswith("IMPORT_MANAGED_")]
    assert all(a.endswith("=NO_DELETE") for a in managed), managed
    assert len(managed) == len(MANAGED)


def test_a_declared_user_keeps_its_hand_given_groups_and_realm_roles():
    argv = _argv()
    assert "IMPORT_USERS_MERGE_GROUPS=true" in argv
    assert "IMPORT_USERS_MERGE_ROLES=true" in argv
    assert [a for a in argv if a.startswith("IMPORT_USERS_")] == [
        "IMPORT_USERS_MERGE_GROUPS=true", "IMPORT_USERS_MERGE_ROLES=true"]
