"""The backup carries the volumes of the monitoring stacks (Gatus, Healthchecks,
Beszel) as it carries every application's data, and names their SQLite
databases so each is also copied whole.

Patterns are matched the way restic 0.19.1 documents: component by component,
`*` and `?` stay inside a component, `**` spans any number of components, a
leading `/` anchors the pattern at the root and any other pattern may start at
any depth, and a directory a pattern matches leaves out everything below it.

Run: uv run pytest tests/unit/test_backup_keeps_monitoring_volumes.py
"""
from __future__ import annotations

import fnmatch
import re

import jinja2
import jinja2.nativetypes
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
BACKUP = ROLES / "backup" / "defaults" / "main.yml"
INFRA = ROLES / "infrastructure"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
VOLUMES = "/srv/catena/docker/volumes"

# app -> the volumes of its stack, as the catalog compose of the app names them.
APP_VOLUMES = {
    "gatus": ["gatus-data", "config"],
    "healthchecks": ["healthchecks-data"],
    "beszel": ["beszel-hub-data"],
}


def _defaults() -> dict:
    return yaml.safe_load((INFRA / "defaults" / "main.yml").read_text(encoding="utf-8"))


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
    assert not _excludes("*/docker/volumes/gatus-data", f"{VOLUMES}/gatus_gatus-data")
    assert _excludes("*/docker/volumes/gatus_gatus-data", f"{VOLUMES}/gatus_gatus-data/_data/gatus.db")


def test_no_pattern_leaves_out_a_monitoring_volume():
    patterns = yaml.safe_load(BACKUP.read_text(encoding="utf-8"))["backup_exclude_patterns"]
    stacks = _stacks()
    checked = 0
    for app, volumes in APP_VOLUMES.items():
        for volume in volumes:
            for path in (f"{VOLUMES}/{stacks[app]}_{volume}",
                         f"{VOLUMES}/{stacks[app]}_{volume}/_data/data.db"):
                checked += 1
                hit = [p for p in patterns if _excludes(p, path)]
                assert not hit, f"{hit} leave {path} out of the backup"
    assert checked == 8


class _Placeholder(jinja2.ChainableUndefined):
    """A variable the render is not given prints as a word."""

    def __str__(self) -> str:
        return "x"


def _plain(path: Path) -> dict:
    """The defaults of a role that hold no expression."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not (isinstance(v, str) and "{{" in v)}


def _render(path: Path, **values) -> str:
    env = jinja2.Environment(undefined=_Placeholder)
    env.filters["bool"] = bool
    return env.from_string(path.read_text(encoding="utf-8")).render(**values)


def _databases() -> list[str]:
    """infrastructure_sqlite_databases, evaluated against the role's names."""
    expr = _defaults()["infrastructure_sqlite_databases"]
    env = jinja2.nativetypes.NativeEnvironment()
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(pattern, repl, value)
    stacks = _stacks()
    return env.from_string(expr).render(
        **_plain(INFRA / "defaults" / "main.yml"),
        gatus_stack=stacks["gatus"],
        healthchecks_stack=stacks["healthchecks"],
        beszel_hub_stack=stacks["beszel"])


def test_the_copy_list_names_each_monitoring_database():
    """The stack and volume names are those of the three monitors' volumes, so
    the list is stable whether a stack or an app holds them."""
    listing = _render(ROLES / "backup" / "templates" / "backup-sqlite.j2",
                      ansible_managed="", storage_mount_point="/srv/catena",
                      infrastructure_sqlite_databases=_databases())
    lines = [ln for ln in listing.splitlines() if ln and not ln.startswith("#")]
    assert lines == [
        f"{VOLUMES}/gatus_gatus-data/_data/gatus.db",
        f"{VOLUMES}/healthchecks_healthchecks-data/_data/hc.sqlite",
        f"{VOLUMES}/beszel-hub_beszel-hub-data/_data/data.db",
        f"{VOLUMES}/beszel-hub_beszel-hub-data/_data/auxiliary.db",
    ]


def test_the_listed_files_are_the_names_the_defaults_hold():
    defaults = _defaults()
    assert defaults["gatus_sqlite_file"] == "gatus.db"
    assert defaults["healthchecks_sqlite_file"] == "hc.sqlite"
    assert defaults["beszel_hub_sqlite_files"] == ["data.db", "auxiliary.db"]


def test_the_patterns_name_no_monitoring_volume():
    patterns = yaml.safe_load(BACKUP.read_text(encoding="utf-8"))["backup_exclude_patterns"]
    for volume in ("gatus-data", "healthchecks-data", "beszel-hub-data"):
        assert not [p for p in patterns if volume in p], volume
