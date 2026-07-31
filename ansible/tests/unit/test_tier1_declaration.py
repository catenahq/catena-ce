"""roles/tier1_stack writes the declaration the Go engine reads.

/etc/catena/tier1.json is the same specs the role renders as compose, written
verbatim. Two renderings of one dict, so the file the engine applies from
cannot describe a control plane different from the one the Ansible ladder just
applied -- which is the failure this file exists to make impossible while both
apply paths exist.

Run: uv run pytest tests/unit/test_tier1_declaration.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "roles" / "tier1_stack"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _flatten(tasks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "always", "rescue"):
            if isinstance(task.get(key), list):
                out.extend(_flatten(task[key]))
    return out


def _task(fragment: str) -> dict:
    for task in _flatten(yaml.safe_load(TASKS.read_text())):
        if fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"no task named like {fragment!r}")


def test_the_declaration_path_is_not_under_the_stacks_dir():
    """`docker stack deploy` must never be pointed at the directory and find a
    file that is not compose."""
    d = _defaults()
    assert d["tier1_declaration_path"] == "/etc/catena/tier1.json"
    assert not d["tier1_declaration_path"].startswith(d["tier1_stack_dir"])


def test_the_declaration_is_the_specs_verbatim():
    """No second rendering. A transform here would be a second description of
    the control plane, free to drift from the one the ladder applies."""
    content = _task("write the engine declaration")["ansible.builtin.copy"]["content"]
    assert "catena_tier1_specs" in content
    assert "'services'" in content or '"services"' in content
    # to_nice_json, not a template that reshapes the dicts.
    assert "to_nice_json" in content


def test_the_declaration_carries_the_same_0640_root_as_the_stack_file():
    """It names every swarm secret the control plane mounts -- not the
    material, but a map of what to ask for."""
    task = _task("write the engine declaration")["ansible.builtin.copy"]
    assert task["mode"] == "0640"
    assert task["owner"] == "root"
    assert task["group"] == "root"


def test_the_declaration_is_written_under_the_same_completeness_guard():
    """A partial roster must not produce a declaration either. Applied, a file
    holding three of four services is an instruction about the whole plane."""
    tasks = yaml.safe_load(TASKS.read_text())
    guarded = None
    for task in tasks:
        if "block" not in task:
            continue
        names = [t.get("name") or "" for t in task["block"]]
        if any("write the engine declaration" in n for n in names):
            guarded = task
    assert guarded is not None, "the declaration write is not inside the guarded block"
    cond = str(guarded["when"])
    assert "tier1_stack_required" in cond
    assert "difference(_tier1_present) | length == 0" in cond


def test_the_declaration_and_the_stack_file_come_from_one_accumulator():
    stack = _task("write the rendered stack file")["ansible.builtin.copy"]["content"]
    declaration = _task("write the engine declaration")["ansible.builtin.copy"]["content"]
    assert "catena_tier1_specs" in stack
    assert "catena_tier1_specs" in declaration
