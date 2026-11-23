"""An app that leaves `default_apps` is left alone on a host that runs it.

Catena removes only its own wiring of the app: the converge names neither the
app's stack nor its overlay, so no task stops, removes or converts a
running copy, and no task deletes an overlay network, which a running copy's
compose may name as external. The copy stays an ordinary catalog app the admin
updates or removes from the panel.

Run: uv run pytest tests/unit/test_a_dropped_default_app_is_left_alone.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
TREES = [ANSIBLE / name for name in ("playbooks", "reconcile", "bootstrap", "helpers", "scripts")]
SUFFIXES = {".yml", ".yaml", ".j2", ".py", ".sh", ".json"}
DROPPED = "healthchecks"
# The names the converge wired the dropped app by: its stack and overlay, and
# the variables that held its address, network and stack.
WIRING = (f"catena-{DROPPED}", f"{DROPPED}_stack", f"{DROPPED}_network",
          f"{DROPPED}_hostname", f"{DROPPED}_subdomain")
NETWORK_REMOVAL = re.compile(r"network[\"',\s]+(rm|remove|prune)\b|docker_network\b")


def _files():
    for tree in TREES:
        for path in sorted(tree.rglob("*")):
            if path.is_file() and path.suffix in SUFFIXES:
                yield path


def test_the_dropped_app_is_not_a_default_app():
    apps = yaml.safe_load(
        (ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml").read_text())["default_apps"]
    assert DROPPED not in {a["app"] for a in apps}


def test_no_file_names_the_dropped_apps_stack_or_overlay():
    """A task that named the stack could stop, remove or convert the running
    copy; none does, because none names it."""
    checked = 0
    for path in _files():
        text = path.read_text(encoding="utf-8")
        checked += 1
        for name in WIRING:
            assert name not in text, f"{path.relative_to(ANSIBLE)} names {name}"
    assert checked > 100


def test_no_task_deletes_a_network():
    """The swarm role creates the overlays it needs and deletes none, so an
    overlay a running app names as external stays."""
    for path in _files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assert not NETWORK_REMOVAL.search(line), (
                f"{path.relative_to(ANSIBLE)}:{number} deletes a network: {line.strip()}")


def test_the_swarm_role_only_creates_overlays():
    tasks = yaml.safe_load((ANSIBLE / "reconcile" / "roles" / "swarm" / "tasks" / "main.yml").read_text())
    for task in tasks:
        command = task.get("ansible.builtin.command") or {}
        argv = command.split() if isinstance(command, str) else command.get("argv") or []
        if argv[:2] == ["docker", "network"]:
            assert argv[2] == "create", task["name"]
