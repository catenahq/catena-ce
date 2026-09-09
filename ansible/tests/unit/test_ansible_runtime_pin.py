"""One validated ansible-core line, named in three places, held together here.

The operator's controller gets it from pyproject.toml through uv. The host gets
it from bootstrap/roles/ansible_runtime, installed by bootstrap into /opt/catena/ansible.
And playbooks/reconcile.yml refuses to run below a floor.

Three numbers that must agree, and nothing in Ansible or pip would notice if
they stopped: a host would install one version, refuse to run it, and report a
version error on a machine nobody is watching. The bench validates ONE
ansible-core line; this is what keeps there being one.

Run: uv run pytest tests/unit/test_ansible_runtime_pin.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
PYPROJECT = _ANSIBLE / "pyproject.toml"
ROLE_DEFAULTS = _ANSIBLE / "bootstrap" / "roles" / "ansible_runtime" / "defaults" / "main.yml"
RECONCILE = _ANSIBLE / "playbooks" / "reconcile.yml"


def _role() -> dict:
    return yaml.safe_load(ROLE_DEFAULTS.read_text())


def _controller_spec() -> str:
    for line in PYPROJECT.read_text().splitlines():
        stripped = line.strip().strip(",").strip('"')
        if stripped.startswith("ansible-core"):
            return stripped
    raise AssertionError("pyproject.toml no longer pins ansible-core")


def test_the_host_installs_what_the_controller_pins():
    """Same string, not merely a compatible one.

    A host and a controller on different ansible-core minors is two products:
    the same playbook, two module implementations, and only one of them is what
    the bench observed.
    """
    assert _role()["ansible_runtime_spec"] == _controller_spec(), (
        f"bootstrap/roles/ansible_runtime installs {_role()['ansible_runtime_spec']!r} "
        f"and pyproject.toml pins {_controller_spec()!r}"
    )


def test_the_floor_is_inside_what_gets_installed():
    """A floor above the installed ceiling is a host that installs a runtime and
    then refuses it -- and it refuses AFTER bootstrap has reported success."""
    role = _role()
    minimum = role["ansible_runtime_minimum"]
    spec = role["ansible_runtime_spec"]
    lower = re.search(r">=\s*([0-9]+\.[0-9]+)", spec)
    upper = re.search(r"<\s*([0-9]+\.[0-9]+)", spec)
    assert lower and upper, f"cannot read a range out of {spec!r}"

    def key(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    assert key(lower.group(1)) <= key(minimum) < key(upper.group(1)), (
        f"the reconcile floor {minimum} is outside the installed range {spec}"
    )


def test_the_reconcile_refuses_below_the_same_floor():
    plays = yaml.safe_load(RECONCILE.read_text())
    declared = plays[0]["vars"]["catena_reconcile_min_ansible"]
    assert declared == _role()["ansible_runtime_minimum"], (
        f"reconcile.yml refuses below {declared} and bootstrap/roles/ansible_runtime "
        f"declares {_role()['ansible_runtime_minimum']}"
    )


def test_the_refusal_happens_before_any_role_runs():
    """Partway through a converge is the one place this must not surface.

    The assertion has to be in pre_tasks, and it has to be tagged `always`, or a
    tag-scoped run skips the check and keeps the failure mode the check exists
    to remove.
    """
    play = yaml.safe_load(RECONCILE.read_text())[0]
    checks = [t for t in play.get("pre_tasks") or []
              if "ansible.builtin.assert" in t
              and "ansible_version" in str(t["ansible.builtin.assert"])]
    assert checks, "reconcile.yml no longer refuses an old ansible-core up front"
    assert "always" in (checks[0].get("tags") or []), (
        "the version refusal is not tagged `always`, so a --tags run skips it"
    )
