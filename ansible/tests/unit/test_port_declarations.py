"""The port declarations this repo writes, and how the converge wires the
reconciler the catena-admin payload ships.

A declaration's `bind` says which chain can enforce it. A swarm `mode: host`
publish is DNAT'd and never reaches ufw INPUT, so a restricted port published
that way is closed only by a DOCKER-USER guard: declared bind=host, ufw installs
a deny, `rules_unapplied` is 0, every artifact validation reads says the port is
guarded -- and the port answers from off-box.

Run: uv run pytest tests/unit/test_port_declarations.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLES = ANSIBLE / "reconcile" / "roles"
ROLE = ROLES / "public_ports"


def test_the_three_loopback_ports_declare_the_dnat_bind():
    tasks = ROLES / "infrastructure" / "tasks"
    for name in ("gatus.yml", "healthchecks.yml", "beszel.yml"):
        body = (tasks / name).read_text(encoding="utf-8")
        assert '"scope": "loopback", "bind": "docker"' in body, name
        assert '"bind": "host"' not in body, (
            f"{name}: bind=host claims ufw INPUT can enforce this port")


def test_the_two_ui_ports_are_host_only_and_the_lane_declares_tailnet():
    """The panel and Portainer answer this host only: an administrator reaches
    them through an SSH forward that lands on the loopback, and the panel's
    direct login is a password over plain HTTP. The migration lane binds the
    tailnet address and means `tailnet` literally."""
    portainer = (ROLES / "portainer" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert '"scope": "loopback", "bind": "docker", "owner": "portainer"' in portainer
    admin = (ROLES / "catena-admin" / "tasks" / "lanes.yml").read_text(encoding="utf-8")
    assert '"port": catena_admin_ui_port | int,\n           "scope": "loopback"' in admin
    assert '"port": catena_migrate_lane_port | int,\n           "scope": "tailnet"' in admin


def test_validate_asserts_the_unapplied_count():
    """The reconciler's summary carries the rules it could NOT install; the
    count is only useful if something reads it."""
    assert "rules_unapplied" in (ANSIBLE / "playbooks" / "validate.yml").read_text()


def _role_tasks() -> list[dict]:
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))


def test_a_payload_without_the_reconciler_fails_the_converge():
    """An image whose payload has no reconciler would leave DOCKER-USER empty
    on every host it installs."""
    fail = next(t for t in _role_tasks() if "ansible.builtin.fail" in t)
    assert "catena_payload_engines_expected | default(true) | bool" in fail["when"]
    assert "not _pp_unit.stat.exists" in fail["when"]


def test_the_timer_is_enabled_only_when_the_unit_is_on_the_host():
    timer = next(t for t in _role_tasks()
                 if (t.get("ansible.builtin.systemd_service") or {}).get("name")
                 == "catena-public-ports.timer")
    assert timer["when"] == "catena_public_ports_installed | bool"


def _flatten(node) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out += _flatten(item)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out += _flatten(node[key])
    return out


def test_every_start_of_the_reconciler_skips_when_it_is_not_installed():
    """With the payload staged out of band the unit lands after the first
    converge, and a start of a unit systemd has never heard of fails it."""
    starts = []
    for path in sorted(ANSIBLE.rglob("*.yml")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "name: catena-public-ports.service" not in text:
            continue
        for task in _flatten(yaml.safe_load(text)):
            unit = (task.get("ansible.builtin.systemd_service") or {}).get("name")
            if unit != "catena-public-ports.service":
                continue
            starts.append(path.name)
            when = task.get("when")
            conds = when if isinstance(when, list) else [when]
            if path.name == "ufw_lockdown.yml":
                continue  # the lockdown runs on an installed host only
            assert "catena_public_ports_installed | default(true) | bool" in conds, (
                f"{path.relative_to(ANSIBLE)}: {task.get('name')}")
    assert len(starts) >= 7, starts


def test_the_role_runs_right_after_the_payload_that_installs_it():
    for rel in ("playbooks/converge.yml", "playbooks/reconcile.yml"):
        plays = yaml.safe_load((ANSIBLE / rel).read_text(encoding="utf-8"))
        roles = [r["role"] if isinstance(r, dict) else r
                 for p in plays for r in (p.get("roles") or [])]
        assert roles.index("public_ports") == roles.index("payload") + 1, rel
    boundary = yaml.safe_load((ANSIBLE / "boundary.yml").read_text(encoding="utf-8"))
    order = boundary["reconcile_roles"]
    assert order.index("public_ports") == order.index("payload") + 1
