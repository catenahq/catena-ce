"""Catena's own stacks that keep data declare it in an x-catena block, and the
swarm stack helper asks the host for the copy before it deploys.

A newer Healthchecks, Gatus or Beszel hub migrates its SQLite volume on its
first start and the previous image cannot read the result. The block names the
service and volume; catena-stack-update-managed infra-data copy copies the
data of each declared service whose image the new stack file changes, and
puts it back when the move rolls back.

Run: uv run pytest tests/unit/test_infra_data_copy.py
"""
from __future__ import annotations

import jinja2
import pytest
import yaml

from ansible_tree import PLAYBOOKS, panel_vars, role_dir, walk_tasks

TEMPLATES = role_dir("infrastructure") / "templates"
STACK = role_dir("infrastructure") / "tasks" / "swarm_stack.yml"

# Template -> the volume its block names, with the variable values a render
# gives the template.
DECLARING = {
    "healthchecks.compose.yml.j2": "healthchecks-data",
    "gatus.compose.yml.j2": "gatus-data",
    "beszel-hub.compose.yml.j2": "beszel-hub-data",
}


class _Placeholder(jinja2.ChainableUndefined):
    """A variable the render is not given prints as a word, so the file stays
    valid YAML without the roles' defaults."""

    def __str__(self) -> str:
        return "x"


def _render(name: str) -> dict:
    env = jinja2.Environment(undefined=_Placeholder)
    env.filters["bool"] = bool
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    return yaml.safe_load(env.from_string(text).render(
        beszel_hub_data_volume="beszel-hub-data",
        catena_public_surface_deferred=False,
        infrastructure_smtp={"enabled": False, "sender": "x", "implicit_tls": False},
    ))


@pytest.mark.parametrize("name,volume", DECLARING.items())
def test_a_stack_that_keeps_data_declares_it(name, volume):
    doc = _render(name)
    assert doc["x-catena"] == {
        "schema": 2,
        "data": {"app": {"volume": volume, "upgrade": "in_place"}},
    }
    assert "app" in doc["services"], f"{name}: the block names a service the file lacks"
    assert volume in doc["volumes"], f"{name}: the block names a volume the file lacks"


def test_the_block_is_the_only_x_key_of_these_files():
    for name in DECLARING:
        assert [k for k in _render(name) if k.startswith("x-")] == ["x-catena"]


def _stack_tasks() -> list[dict]:
    return walk_tasks(yaml.safe_load(STACK.read_text(encoding="utf-8")))


def _index(fragment: str) -> int:
    for i, task in enumerate(_stack_tasks()):
        if fragment in (task.get("name") or ""):
            return i
    raise AssertionError(f"task not found: {fragment!r}")


def test_the_copy_runs_between_the_stack_file_and_the_deploy():
    copy, write = _index("copy the data a moved image migrates"), _index("write the stack file")
    attempt = _index("deploy with retry on registry transient")
    assert write < copy < attempt


def test_the_copy_asks_the_verb_for_this_stack_and_file():
    task = _stack_tasks()[_index("copy the data a moved image migrates")]
    argv = task["ansible.builtin.command"]["argv"]
    assert argv == [
        "{{ catena_admin_stack_update_bin }}", "infra-data", "copy",
        "--stack", "{{ svc_name }}",
        "--file", "{{ swarm_stack_dir }}/{{ svc_name }}.yml",
        "--run", "{{ lookup('env', 'CATENA_INFRA_RUN') }}",
    ]
    assert panel_vars()["catena_admin_stack_update_bin"].endswith("/catena-stack-update-managed")


def test_a_refusal_fails_the_task_and_a_copy_reports_no_change():
    """No failed_when and no ignore_errors: a refused copy stops the converge
    before the deploy, so a stack is never deployed onto data with no copy."""
    task = _stack_tasks()[_index("copy the data a moved image migrates")]
    assert task["changed_when"] is False
    for key in ("failed_when", "ignore_errors", "when"):
        assert key not in task, f"{key} lets a stack deploy onto data with no copy"


def test_every_caller_of_the_helper_runs_after_the_payload_that_installs_the_verb():
    reconcile = yaml.safe_load((PLAYBOOKS / "reconcile.yml").read_text(encoding="utf-8"))[0]
    roles = [r["role"] if isinstance(r, dict) else r for r in reconcile["roles"]]
    for caller in ("keycloak", "oauth2_proxy", "infrastructure"):
        assert roles.index("payload") < roles.index(caller)
