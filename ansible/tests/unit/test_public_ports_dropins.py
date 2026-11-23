"""reconcile/roles/public_ports installs two drop-ins that start the reconciler:
on every docker.service start, and 20 seconds after every tailscaled.service
start. Each rendered drop-in passes its own validate check, the two dispatch
under different transient unit names so one never refuses the other, and both
are paths the role manages.

Run: uv run pytest tests/unit/test_public_ports_dropins.py
"""
from __future__ import annotations

import re

import jinja2
import yaml

from ansible_jinja import b64, holds
from ansible_tree import role_dir

ROLE = role_dir("public_ports")
TASKS = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
VALIDATE = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
ASSERT = "ansible.builtin.assert"


def _named(tasks: list[dict], fragment: str) -> dict:
    for t in tasks:
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _render(unit: str) -> str:
    task = _named(TASKS, f"{unit}.service.d drop-in (")
    src = task["ansible.builtin.template"]["src"]
    text = jinja2.Environment(loader=jinja2.FileSystemLoader(ROLE / "templates")).get_template(src).render(
        ansible_managed="managed")
    assert task["ansible.builtin.template"]["dest"] == f"/etc/systemd/system/{unit}.service.d/30-catena-public-ports.conf"
    return text


def _unit_name(text: str) -> str:
    match = re.search(r"--unit=(\S+)", text)
    assert match, text
    return match.group(1)


def test_each_dropin_passes_its_own_validate_check():
    assert holds(_named(VALIDATE, "docker start re-applies the rules"), ASSERT,
                 _pp_val_dropin={"content": b64(_render("docker"))})
    assert holds(_named(VALIDATE, "tailscaled start re-applies the rules"), ASSERT,
                 _pp_val_ts_dropin={"content": b64(_render("tailscaled"))})


def test_the_two_dropins_dispatch_under_different_unit_names():
    assert _unit_name(_render("docker")) != _unit_name(_render("tailscaled"))


def test_the_tailscaled_dropin_waits_for_tailscaled_to_place_its_own_jumps():
    text = _render("tailscaled")
    assert "--on-active=20" in text
    assert re.search(r"^ExecStartPost=-", text, re.M)


def test_both_dropins_are_managed_paths():
    assert set(DEFAULTS["public_ports_managed_paths"]) >= {
        "/etc/systemd/system/docker.service.d/30-catena-public-ports.conf",
        "/etc/systemd/system/tailscaled.service.d/30-catena-public-ports.conf",
    }


def test_a_changed_dropin_reloads_systemd():
    reload = _named(TASKS, "daemon-reload if a drop-in changed")
    assert reload["when"] == "_pp_docker_dropin.changed or _pp_tailscaled_dropin.changed"
