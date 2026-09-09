"""Every directory the panel bind-mounts must be created before it deploys.

Swarm REJECTS a task whose bind source is missing -- it does not create the
path. Under --restart-condition=any a rejected task is replaced every few
seconds forever, so a missing source is not an error the converge sees: it is
an install parked on "create the swarm service (first run)" with no output.

That is what /var/backups/catena-export did. Its contents belong to
roles/backup, which runs at site.yml position 23 -- TWO roles after
catena-admin at 21 -- so on a first converge the directory the panel mounts
did not exist yet, and every task swarm placed was rejected on sight:

    "invalid mount config for type \\"bind\\": bind source path does not
     exist: /var/backups/catena-export"

The test bench never caught it because it sets catena_admin_deploy_from_converge
false and deploys the panel out of band AFTER the converge, by which point
roles/backup has made the directory. The converge-time create at role 21 is
the path clients take and the one the bench does not exercise.

Run: uv run pytest tests/unit/test_catena_admin_bind_sources_exist.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
PLUGIN = _ANSIBLE / "playbooks" / "filter_plugins" / "catena_admin_service.py"
HOST_TASKS = _ANSIBLE / "roles" / "catena_admin_host" / "tasks" / "main.yml"
COMMON_DEFAULTS = _ANSIBLE / "roles" / "common" / "defaults" / "main.yml"
BACKUP_DEFAULTS = _ANSIBLE / "roles" / "backup" / "defaults" / "main.yml"

EXPORT_DIR = "/var/backups/catena-export"


def _plugin():
    spec = importlib.util.spec_from_file_location("catena_admin_service", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _file_task_paths() -> set[str]:
    """Every path host.yml creates with the file module as a directory."""
    out = set()
    for task in yaml.safe_load(HOST_TASKS.read_text()) or []:
        f = task.get("ansible.builtin.file") or {}
        if f.get("state") == "directory" and f.get("path"):
            out.add(str(f["path"]))
    return out


def test_the_export_dir_is_created_before_the_panel_mounts_it():
    assert "{{ catena_export_dir }}" in _file_task_paths(), (
        "host.yml runs before deploy.yml in the same role; without this task "
        "the first-run service create is rejected on a loop and the install "
        "hangs with no output"
    )


def test_the_export_dir_is_still_mounted():
    """If the mount ever goes away this whole test file is dead weight -- but
    a silently-dropped mount is the /recovery tab rendering empty, so pin it."""
    argv = _plugin().catena_admin_service_argv({
        "name": "catena-admin",
        "image": "ghcr.io/catenahq/catena-admin:latest",
        "network": "catena-network",
        "ui_port": "9010",
        "direct_port": "8001",
    })
    assert (f"--mount=type=bind,source={EXPORT_DIR},"
            f"destination={EXPORT_DIR},readonly") in argv


def test_one_literal_for_the_path_across_the_two_roles():
    """roles/backup creates the same directory two roles later. Two literals
    is how they drift, and a drifted path is a bind source that exists under
    the other name -- the same rejection loop, harder to read."""
    common = yaml.safe_load(COMMON_DEFAULTS.read_text())
    backup = yaml.safe_load(BACKUP_DEFAULTS.read_text())
    assert common["catena_export_dir"] == EXPORT_DIR
    assert backup["backup_export_dir"] == "{{ catena_export_dir }}"


def test_both_creators_agree_on_ownership():
    """The panel joins gid 1000 as a supplementary group to read this dir. If
    the two creators disagree, whichever runs last wins and the /recovery tab
    is empty on exactly one of the two orderings."""
    common = yaml.safe_load(COMMON_DEFAULTS.read_text())
    assert common["catena_export_dir_group"] == "1000"
    assert common["catena_export_dir_mode"] == "0750"
    for task in yaml.safe_load(HOST_TASKS.read_text()) or []:
        f = task.get("ansible.builtin.file") or {}
        if f.get("path") == "{{ catena_export_dir }}":
            assert f["owner"] == "root"
            assert f["group"] == "{{ catena_export_dir_group }}"
            assert f["mode"] == "{{ catena_export_dir_mode }}"
            return
    raise AssertionError("host.yml does not create the export dir")
