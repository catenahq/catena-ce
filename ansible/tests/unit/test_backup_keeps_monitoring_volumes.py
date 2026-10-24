"""The backup carries the volumes of the monitoring stacks (Gatus, Healthchecks,
Beszel) as it carries every application's data.

Patterns are matched the way restic 0.19.1 documents: component by component,
`*` and `?` stay inside a component, `**` spans any number of components, a
leading `/` anchors the pattern at the root and any other pattern may start at
any depth, and a directory a pattern matches leaves out everything below it.

Run: uv run pytest tests/unit/test_backup_keeps_monitoring_volumes.py
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

ROLES = Path(__file__).resolve().parents[2] / "reconcile" / "roles"
BACKUP = ROLES / "backup" / "defaults" / "main.yml"
INFRA = ROLES / "infrastructure"
VOLUMES = "/srv/catena/docker/volumes"

STACKS = {
    "gatus.compose.yml.j2": "gatus_compose_name",
    "healthchecks.compose.yml.j2": "healthchecks_compose_name",
    "beszel-hub.compose.yml.j2": "beszel_hub_compose_name",
}


def _defaults() -> dict:
    return yaml.safe_load((INFRA / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _volume_keys(template: str) -> list[str]:
    """The keys of the compose template's top-level `volumes:` mapping, with a
    `{{ name }}` key resolved from the infrastructure defaults."""
    text = (INFRA / "templates" / template).read_text(encoding="utf-8")
    block = re.search(r"^volumes:\n((?:[ \t]+.*\n|\n)*)", text, re.MULTILINE).group(1)
    defaults = _defaults()
    keys = []
    for key in re.findall(r"^  ([^\s#][^:]*):\s*$", block, re.MULTILINE):
        var = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", key)
        keys.append(defaults[var.group(1)] if var else key)
    return keys


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
    assert not _excludes("*/docker/volumes/gatus-data", f"{VOLUMES}/gatus_gatus-data")
    assert _excludes("*/docker/volumes/gatus_gatus-data", f"{VOLUMES}/gatus_gatus-data/_data/gatus.db")


def test_no_pattern_leaves_out_a_monitoring_volume():
    patterns = yaml.safe_load(BACKUP.read_text(encoding="utf-8"))["backup_exclude_patterns"]
    defaults = _defaults()
    checked = 0
    for template, name in STACKS.items():
        for volume in _volume_keys(template):
            for path in (f"{VOLUMES}/{defaults[name]}_{volume}",
                         f"{VOLUMES}/{defaults[name]}_{volume}/_data/data.db"):
                checked += 1
                hit = [p for p in patterns if _excludes(p, path)]
                assert not hit, f"{hit} leave {path} out of the backup"
    assert checked == 6


def test_the_patterns_name_no_monitoring_volume():
    patterns = yaml.safe_load(BACKUP.read_text(encoding="utf-8"))["backup_exclude_patterns"]
    for volume in ("gatus-data", "healthchecks-data", "beszel-hub-data"):
        assert not [p for p in patterns if volume in p], volume
