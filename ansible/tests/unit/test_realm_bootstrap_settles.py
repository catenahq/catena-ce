"""The converge that bootstraps the Keycloak realm leaves it settled, and the
realm files hold their passwords where only root reads them.

realm_bootstrap.yml renders realm-vps.yaml.j2 twice. The persistent file is
imported on every converge and renders from the store alone, so the next
converge renders the same bytes and reports nothing to change. The seed (the
realm admin's password and the realm's tunable settings) is rendered into its
own dir, imported once, deleted, and only then are the bootstrap markers
written. Every import runs with keycloak-config-cli's cache off. Settings > Mail owns the realm's mail settings:
they render into the persistent file on every converge, and an empty map when
mail is off.

The template is rendered here the way the converge renders it, with stand-ins
for the two Ansible filters it uses.

Run: uv run pytest tests/unit/test_realm_bootstrap_settles.py
"""
from __future__ import annotations

import json
from pathlib import Path

import jinja2
import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "keycloak"
TEMPLATE = ROLE / "templates" / "realm-vps.yaml.j2"
BOOTSTRAP = ROLE / "tasks" / "realm_bootstrap.yml"
IMPORT = ROLE / "tasks" / "_config_cli_import.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

ADMIN_PASSWORD = "admin-pw-0123456789"
SMTP_PASSWORD = 're_key"with$odd'

SEEDED_ONCE = (
    "resetPasswordAllowed", "rememberMe", "verifyEmail", "loginWithEmailAllowed",
    "permanentLockout", "maxFailureWaitSeconds", "minimumQuickLoginWaitSeconds",
    "waitIncrementSeconds", "quickLoginCheckMilliSeconds", "maxDeltaTimeSeconds",
    "failureFactor", "defaultGroups", "attributes",
)


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _render(*, realm_seed, smtp_enabled: bool = True) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = _bool
    env.filters["to_json"] = json.dumps
    return env.from_string(TEMPLATE.read_text()).render(
        ansible_managed="managed",
        realm_seed=realm_seed,
        keycloak_realm="vps",
        keycloak_realm_display_name="acme.test",
        keycloak_enforce_mfa="false",
        admin_email="admin@acme.test",
        admin_password=ADMIN_PASSWORD,
        smtp_enabled=smtp_enabled,
        smtp_host="smtp.resend.com",
        smtp_port=587,
        smtp_user="resend",
        smtp_from="no-reply@acme.test",
        smtp_password=SMTP_PASSWORD,
        smtp_use_starttls=True,
    )


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text()) if isinstance(t, dict)]


def _named(tasks: list[dict], fragment: str) -> dict:
    return next(t for t in tasks if fragment in str(t.get("name", "")))


def _persistent_render_task() -> dict:
    return next(
        t for t in _tasks(BOOTSTRAP)
        if "realm-vps.yaml.j2" in str((t.get("ansible.builtin.template") or {}).get("src", "")))


def _seed_block() -> dict:
    return _named(_tasks(BOOTSTRAP), "seed the admin password")


def test_the_persistent_render_depends_on_no_marker():
    """The same bytes whether the bootstrap markers exist or not: the render
    takes realm_seed as a literal, and the template reads nothing else that a
    marker could change."""
    task = _persistent_render_task()
    assert task["ansible.builtin.template"]["dest"].startswith("{{ keycloak_realm_host_dir }}")
    assert task["vars"] == {"realm_seed": False}
    assert "_kc_" not in TEMPLATE.read_text(), "the template reads a marker register"
    assert _render(realm_seed=task["vars"]["realm_seed"]) == _render(realm_seed=False)


def test_the_persistent_file_carries_no_seed():
    rendered = _render(realm_seed=False)
    doc = yaml.safe_load(rendered)
    assert all("credentials" not in user for user in doc["users"])
    assert ADMIN_PASSWORD not in rendered
    for key in SEEDED_ONCE:
        assert key not in doc, f"{key} is asserted on every converge"


def test_the_seed_carries_the_admin_password_and_the_tunables():
    doc = yaml.safe_load(_render(realm_seed=True))
    creds = doc["users"][0]["credentials"]
    assert creds == [{"type": "password", "value": ADMIN_PASSWORD, "temporary": False}]
    for key in SEEDED_ONCE:
        assert key in doc, f"{key} is missing from the seed"


@pytest.mark.parametrize("realm_seed", [False, True])
def test_the_mail_settings_follow_the_store(realm_seed):
    smtp = yaml.safe_load(_render(realm_seed=realm_seed))["smtpServer"]
    assert smtp["host"] == "smtp.resend.com"
    assert smtp["port"] == "587"
    assert smtp["user"] == "resend"
    assert smtp["from"] == "no-reply@acme.test"
    assert smtp["password"] == SMTP_PASSWORD
    assert smtp["starttls"] == "true" and smtp["ssl"] == "false"


def test_mail_turned_off_clears_the_realm_mail_settings():
    """keycloak-config-cli sends the empty map through (its clone keeps
    non-null values) and Keycloak replaces the realm's SMTP config with it."""
    doc = yaml.safe_load(_render(realm_seed=False, smtp_enabled=False))
    assert doc["smtpServer"] == {}


def test_no_smtp_marker_remains():
    assert "keycloak-realm-smtp-bootstrapped" not in BOOTSTRAP.read_text()
    assert "smtp_render_password" not in TEMPLATE.read_text()


def test_the_seed_is_imported_alone_then_deleted():
    block = _seed_block()
    assert "_kc_admin_pw_marker" in block["when"] and "_kc_settings_marker" in block["when"]
    steps = block["block"]
    render = _named(steps, "render the realm seed")
    assert render["ansible.builtin.template"]["dest"].startswith("{{ keycloak_realm_seed_dir }}")
    assert render["vars"] == {"realm_seed": True}
    imp = _named(steps, "import the realm seed")
    assert imp["ansible.builtin.include_tasks"]["file"] == "_config_cli_import.yml"
    assert imp["vars"]["kc_import_dir"] == "{{ keycloak_realm_seed_dir }}"
    delete = block["always"][0]["ansible.builtin.file"]
    assert delete["state"] == "absent"
    assert delete["path"] == render["ansible.builtin.template"]["dest"]


def test_the_markers_are_written_after_the_seed_import():
    steps = _seed_block()["block"]
    names = [str(t.get("name", "")) for t in steps]
    imported = next(i for i, n in enumerate(names) if "import the realm seed" in n)
    for marker in ("keycloak-realm-admin-bootstrapped", "keycloak-realm-settings-bootstrapped"):
        at = next(i for i, t in enumerate(steps)
                  if marker in str((t.get("ansible.builtin.file") or {}).get("path", "")))
        assert at > imported, f"{marker} is written before the seed import"
    assert "bootstrapped" not in json.dumps(_seed_block()["always"])


def test_the_persistent_import_follows_the_render():
    imp = _named(_tasks(BOOTSTRAP), "import the realm config")
    assert imp["vars"]["kc_import_dir"] == "{{ keycloak_realm_host_dir }}"
    assert "_keycloak_realm_render is changed" in imp["vars"]["kc_import_rendered"]


def test_every_import_applies_its_files_whole():
    """The import cache off: an edit made in the realm's admin UI does not
    outlast an import of a file that did not change."""
    argv = _tasks(IMPORT)[0]["ansible.builtin.command"]["argv"]
    assert "IMPORT_CACHE_ENABLED=false" in argv
    mount = next(a for a in argv if str(a).endswith(":/config:ro"))
    assert mount.startswith("{{ kc_import_dir }}")


def test_the_realm_files_sit_under_a_private_parent():
    """The persistent file carries the SMTP password and the seed the admin
    password; the 0700 parent is what keeps them from other accounts."""
    defaults = yaml.safe_load(DEFAULTS.read_text())
    for key in ("keycloak_realm_host_dir", "keycloak_realm_seed_dir"):
        assert defaults[key].startswith("{{ keycloak_config_dir }}/"), key
    tasks = _tasks(BOOTSTRAP)
    private = next(i for i, t in enumerate(tasks)
                   if (t.get("ansible.builtin.file") or {}).get("path") == "{{ keycloak_config_dir }}")
    assert tasks[private]["ansible.builtin.file"]["mode"] == "0700"
    realm_dir = next(i for i, t in enumerate(tasks)
                     if (t.get("ansible.builtin.file") or {}).get("path") == "{{ keycloak_realm_host_dir }}")
    assert private < realm_dir
