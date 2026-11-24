"""The host paths catena-ce renders and installs are the contract's.

The contract (contracts host-layout/layout.json) writes once the folders and
files a server holds. catena-ce keeps its own values, because the converge runs
on hosts with no contracts checkout; this test holds each one to the contract:

- the storage mount, the Docker data root, the storage role's folders and the
  backup set are the contract's roots, data folders and backup set;
- each file a role writes, or names for another component, is the contract's
  path of that name;
- the paths a validate task or the disk-usage action keep as literals are under
  a contract root and, where the contract names them, are its values.

Run: uv run pytest tests/unit/test_host_layout.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
REPO = ANSIBLE.parent
LAYOUT = REPO.parent / "contracts" / "host-layout" / "layout.json"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
BOOTSTRAP = ANSIBLE / "bootstrap" / "roles"
RECONCILE = ANSIBLE / "reconcile" / "roles"

CONTRACT = json.loads(LAYOUT.read_text())
ROOTS = CONTRACT["roots"]
DATA = CONTRACT["data_folders"]
FILES = CONTRACT["files"]

# A host path under a Catena root, as a shell word or a YAML value spells it.
LITERAL = re.compile(r"/(?:srv|etc|var/lib)/catena(?![\w-])(?:/[\w.-]+)*")


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _group_vars() -> dict:
    return _yaml(GROUP_VARS)


def _defaults(role: Path) -> dict:
    return _yaml(role / "defaults" / "main.yml")


def _render(text: str, **variables) -> str:
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(text).render(**variables)


def _storage() -> dict:
    return {"storage_mount_point": _group_vars()["storage_mount_point"]}


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


# --- the roots and the data folders -------------------------------------------

def test_the_storage_mount_is_the_data_root():
    assert _group_vars()["storage_mount_point"] == ROOTS["data"]


def test_the_panels_stats_and_state_folders_are_the_contracts():
    gv = _group_vars()
    assert gv["catena_admin_stats_dir"] == ROOTS["state"]
    assert gv["catena_admin_state_dir"] == FILES["admin_state"]
    assert _defaults(RECONCILE / "backup")["backup_stats_dir"] == ROOTS["state"]


def test_the_storage_role_makes_the_contracts_data_folders():
    names = [d["name"] for d in _defaults(BOOTSTRAP / "storage")["storage_data_subdirs"]]
    made = {f"{ROOTS['data']}/{name}" for name in names}
    assert len(names) == len(made)
    assert made == {DATA["apps"], DATA["compose"], DATA["backup_staging"], DATA["docker"]}


def test_the_docker_data_root_is_the_contracts_docker_folder():
    root = _defaults(BOOTSTRAP / "docker")["docker_data_root"]
    assert _render(root, **_storage()) == DATA["docker"]


def test_the_backup_product_paths_are_the_contracts_backup_set():
    paths = _defaults(RECONCILE / "backup")["backup_product_paths"]
    assert [_render(p, **_storage()) for p in paths] == CONTRACT["backup_set"]


# --- the files ----------------------------------------------------------------

# The contract's file each role variable holds.
FILE_VARS = {
    "daily_env": "catena_daily_env_path",
    "stack_update_env": "catena_stack_update_env_path",
    "managed_services": "catena_managed_services_path",
    "backup_env": "backup_env_file",
    "backup_stats": "backup_stats_file",
    "backup_held": "backup_held_record",
    "restic_pass": "backup_password_file",
    "stacks": ("swarm_stack_dir", "tier1_stack_dir"),
    "release": "catena_release_manifest_path",
    "payload_manifest": "catena_payload_manifest_path",
    "converge_manifest": "catena_converge_manifest_path",
    "bootstrap_drift": "catena_bootstrap_drift_path",
    "cloudflare_zones": "catena_zone_projection_path",
    "ee_payload_root": "catena_payload_stage_dir",
    "payload_image": "catena_payload_marker",
    "version_check": "version_check_output_file",
    "version_check_overrides": "version_check_overrides_file",
    "default_apps": "default_apps_list_path",
    "uptime_targets": "uptime_targets_path",
    "dashboard_sync_env": "dashboard_sync_env_path",
    "heartbeat_env": "heartbeat_env_file",
    "keycloak_master_admins": "keycloak_master_admins_path",
    "admin_ssh": "catena_admin_host_ssh_dir",
    "admin_actions": "catena_admin_actions_yml_path",
    "admin_actions_d": "catena_admin_actions_overlay_dir",
    "admin_allowed": "catena_admin_allowed_actions_path",
    "extra_tiles": "catena_admin_extra_tiles_path",
    "public_ports_d": "public_ports_fragment_dir",
    "traefik": "traefik_config_dir",
}


def _file(key: str, holder: str) -> str:
    """The contract's path for key, which catena-ce holds in holder: a key the
    contract lacks fails here."""
    assert key in FILES, f"the contract has no files entry {key!r}, which catena-ce holds in {holder}"
    return FILES[key]


def _variables() -> dict:
    merged: dict = {}
    for defaults in sorted(ANSIBLE.glob("*/roles/*/defaults/main.yml")):
        merged.update(_yaml(defaults) or {})
    merged.update(_group_vars())
    return merged


def test_each_file_a_role_names_is_the_contracts_path():
    variables = _variables()
    for key, names in FILE_VARS.items():
        for name in (names,) if isinstance(names, str) else names:
            value = variables[name]
            if "{{" in value:
                value = _render(value, **variables)
            assert value == _file(key, name), (key, name)


# Files a task or a script spells out as a literal: the contract key and the
# file that holds the path.
FILE_LITERALS = (
    ("backup_paths", "reconcile/roles/backup/tasks/install.yml"),
    ("backup_exclude_patterns", "reconcile/roles/backup/tasks/install.yml"),
    ("backup_sqlite", "reconcile/roles/backup/tasks/install.yml"),
    ("backup_coverage_paths", "reconcile/roles/backup/tasks/install.yml"),
    ("keycloak_realm_admin_bootstrapped", "reconcile/roles/keycloak/tasks/realm_bootstrap.yml"),
    ("keycloak_realm_settings_bootstrapped", "reconcile/roles/keycloak/tasks/realm_bootstrap.yml"),
    ("version_stamp", "bootstrap/roles/common/tasks/validate.yml"),
    ("traefik_dynamic", "reconcile/roles/infrastructure/tasks/validate.yml"),
    ("public_ports_summary", "playbooks/validate.yml"),
    ("install_logs", "install-host.sh"),
)


def test_each_file_a_task_spells_out_is_the_contracts_path():
    for key, relative in FILE_LITERALS:
        assert _file(key, relative) in (ANSIBLE / relative).read_text(), (key, relative)


def test_the_recovery_readmes_are_written_at_the_contracts_paths():
    install = _yaml(RECONCILE / "backup" / "tasks" / "install.yml")
    [task] = [t for t in install if t.get("name", "").startswith("Sovereign-exit: drop the manual-rebuild README")]
    dest = task["ansible.builtin.template"]["dest"]
    for key, language in (("recovery_readme_en", "en"), ("recovery_readme_fr", "fr")):
        assert _file(key, "backup/tasks/install.yml") == _render(dest, item=language), key


def test_the_env_files_are_written_where_the_role_defaults_say():
    lanes = _yaml(RECONCILE / "catena-admin" / "tasks" / "lanes.yml")
    dests = {t["ansible.builtin.template"]["src"]: t["ansible.builtin.template"]["dest"]
             for t in lanes if "ansible.builtin.template" in t}
    assert dests["daily.env.j2"] == "{{ catena_daily_env_path }}"
    assert dests["stack-update.env.j2"] == "{{ catena_stack_update_env_path }}"
    assert dests["managed-services.json.j2"] == "{{ catena_managed_services_path }}"
    install = _yaml(RECONCILE / "backup" / "tasks" / "install.yml")
    written = [t["ansible.builtin.template"]["dest"] for t in install
               if "ansible.builtin.template" in t and t["ansible.builtin.template"].get("src") == "backup.env.j2"]
    assert written == ["{{ backup_env_file }}"]


def test_the_stack_update_env_names_the_contracts_spec_list():
    text = (RECONCILE / "catena-admin" / "templates" / "stack-update.env.j2").read_text()
    admin = _defaults(RECONCILE / "catena-admin")
    value = re.search(r"^MANAGED_SERVICES_FILE=(.*)$", text, re.M).group(1)
    assert _render(value, **admin) == FILES["managed_services"]


def test_the_daily_env_names_the_contracts_staging_folder():
    text = (RECONCILE / "catena-admin" / "templates" / "daily.env.j2").read_text()
    value = re.search(r"^DAILY_PREFLIGHT_STAGING_DIR=(.*)$", text, re.M).group(1)
    assert _render(value, **_storage()) == DATA["backup_staging"]


def test_the_restic_folders_are_the_contracts_and_the_snapshot_skips_them():
    install = _yaml(RECONCILE / "backup" / "tasks" / "install.yml")
    [make] = [t for t in install
              if t.get("name") == "Ensure restic cache + home dirs exist"]
    assert make["loop"] == [FILES["restic_home"], FILES["restic_cache"]]
    patterns = _defaults(RECONCILE / "backup")["backup_exclude_patterns"]
    for key in ("restic_home", "restic_cache"):
        assert FILES[key] in patterns, key
        assert f"{FILES[key]}/*" in patterns, key


def test_the_snapshot_skips_the_contracts_prebump_folder_copies():
    patterns = _defaults(RECONCILE / "backup")["backup_exclude_patterns"]
    name = Path(FILES["prebump"]).name
    assert f"*/{name}/infra-*" in patterns
    assert _under(FILES["prebump"], ROOTS["state"])


def test_each_helper_reads_the_store_at_the_contracts_path():
    for relative, name in (("helpers/onbox_config.py", "DEFAULT_STORE_PATH"),
                           ("helpers/fetch_release.py", "DEFAULT_CONFIG"),
                           ("scripts/catena-restic-key.py", "STORE_PATH")):
        text = (ANSIBLE / relative).read_text()
        value = re.search(rf'^{name} = "([^"]+)"', text, re.M).group(1)
        assert value == FILES["config_store"], relative


# --- the literals --------------------------------------------------------------

def _checked_literal(path: str, where: str) -> None:
    """A literal is a contract path, or one folder or file below one of its roots
    or data folders."""
    known = set(ROOTS.values()) | set(DATA.values()) | set(FILES.values())
    if path in known:
        return
    assert any(_under(path, root) for root in ROOTS.values()), f"{where}: {path} is under no contract root"
    if _under(path, ROOTS["data"]):
        assert any(_under(path, folder) for folder in DATA.values()), (
            f"{where}: {path} is under the data root and no data folder")


def test_the_literals_of_the_validate_tasks_are_the_contracts():
    seen = 0
    for validate in sorted(ANSIBLE.glob("*/roles/*/tasks/validate.yml")):
        where = str(validate.relative_to(ANSIBLE))
        for path in LITERAL.findall(validate.read_text()):
            _checked_literal(path, where)
            seen += 1
    assert seen


def test_the_storage_validation_lists_the_contracts_data_folders():
    text = (BOOTSTRAP / "storage" / "tasks" / "validate.yml").read_text()
    literals = set(LITERAL.findall(text))
    assert literals == {ROOTS["data"], DATA["apps"], DATA["compose"], DATA["backup_staging"], DATA["docker"]}


def test_the_docker_validation_checks_the_contracts_data_root():
    text = (BOOTSTRAP / "docker" / "tasks" / "validate.yml").read_text()
    assert set(LITERAL.findall(text)) == {DATA["docker"]}


def test_the_disk_usage_action_reads_the_contracts_data_root():
    text = (RECONCILE / "catena-admin" / "templates" / "actions.yml.j2").read_text()
    start = text.index("- name: disk-usage")
    entry = text[start:text.index("\n  - name:", start + 1)]
    literals = set(LITERAL.findall(entry))
    assert literals == {ROOTS["data"]}
    for path in LITERAL.findall(text):
        _checked_literal(path, "actions.yml.j2")
