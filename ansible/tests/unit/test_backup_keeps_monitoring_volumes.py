"""The backup carries the volumes of the monitoring stack (Beszel) as
it carries every application's data.

Patterns are matched the way restic 0.19.1 documents: component by component,
`*` and `?` stay inside a component, `**` spans any number of components, a
leading `/` anchors the pattern at the root and any other pattern may start at
any depth, and a directory a pattern matches leaves out everything below it.

Run: uv run pytest tests/unit/test_backup_keeps_monitoring_volumes.py
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
BACKUP = ROLES / "backup" / "defaults" / "main.yml"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
WIRING = ANSIBLE.parents[1] / "contracts" / "default-apps" / "wiring.json"
VOLUMES = "/srv/catena/docker/volumes"


def _wiring() -> dict:
    return json.loads(WIRING.read_text(encoding="utf-8"))["apps"]


# app -> the docker volume names of its stack, from the wiring contract.
APP_VOLUMES = {app: list(entry["volumes"].values()) for app, entry in _wiring().items()}


def _stacks() -> dict[str, str]:
    """app -> stack, from the default apps."""
    apps = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))["default_apps"]
    return {a["app"]: a["stack"] for a in apps}


def _components_match(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_components_match(pattern[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _components_match(pattern[1:], parts[1:])


def _excludes(pattern: str, path: str) -> bool:
    """True when restic leaves `path` out for `pattern`: the pattern matches the
    path or one of its parent directories."""
    parts = path.strip("/").split("/")
    want = pattern.strip("/").split("/")
    for end in range(1, len(parts) + 1):
        head = parts[:end]
        if pattern.startswith("/"):
            if _components_match(want, head):
                return True
        elif any(_components_match(want, head[start:]) for start in range(len(head))):
            return True
    return False


def test_the_matching_follows_restics_rules():
    assert _excludes("*/prebump/infra-*", "/var/lib/catena/prebump/infra-7")
    assert _excludes("*/prebump/infra-*", "/var/lib/catena/prebump/infra-7/x/y")
    assert _excludes("**/postmaster.pid", "/srv/catena/docker/volumes/pg/_data/pgdata/postmaster.pid")
    assert _excludes("/etc/ssh/ssh_host_*", "/etc/ssh/ssh_host_ed25519_key")
    assert not _excludes("/etc/ssh/ssh_host_*", "/srv/etc/ssh/ssh_host_ed25519_key")
    assert not _excludes("*/docker/volumes/app-data", f"{VOLUMES}/stack_app-data")
    assert _excludes("*/docker/volumes/stack_app-data", f"{VOLUMES}/stack_app-data/_data/app.db")


def test_no_pattern_leaves_out_a_monitoring_volume():
    patterns = yaml.safe_load(BACKUP.read_text(encoding="utf-8"))["backup_exclude_patterns"]
    checked = 0
    for volumes in APP_VOLUMES.values():
        for volume in volumes:
            for path in (f"{VOLUMES}/{volume}", f"{VOLUMES}/{volume}/_data/data.db"):
                checked += 1
                hit = [p for p in patterns if _excludes(p, path)]
                assert not hit, f"{hit} leave {path} out of the backup"
    assert checked == 2 * sum(len(volumes) for volumes in APP_VOLUMES.values()) > 0


def test_the_contract_names_each_volume_after_its_stack():
    stacks = _stacks()
    for app, entry in _wiring().items():
        assert entry["stack"] == stacks[app]
        for key, name in entry["volumes"].items():
            assert name == f"{stacks[app]}_{key}", (app, key, name)


def test_the_patterns_name_no_monitoring_volume():
    patterns = yaml.safe_load(BACKUP.read_text(encoding="utf-8"))["backup_exclude_patterns"]
    for entry in _wiring().values():
        for volume in entry["volumes"]:
            assert not [p for p in patterns if volume in p], volume
