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


# --- two roles, one directory ------------------------------------------------
#
# roles/infrastructure (swarm_stack.yml) creates /etc/catena/stacks for the
# Portainer stack files; roles/tier1_stack creates it for the tier-1 render.
# They declared different modes -- 0750 and 0755 -- so every converge reset
# what the previous one set and BOTH reported changed. The idempotency gate
# could never go green: observed as a permanent changed=2 on bench run 3642,
# on a host where nothing else had drifted.

INFRA_SWARM_STACK = (
    ANSIBLE / "roles" / "infrastructure" / "tasks" / "swarm_stack.yml"
)


def _dir_task_mode(path: Path, fragment: str) -> str:
    for task in _flatten(yaml.safe_load(path.read_text())):
        if fragment in (task.get("name") or ""):
            spec = task.get("ansible.builtin.file") or {}
            if spec.get("state") == "directory":
                return str(spec.get("mode"))
    raise AssertionError(f"no directory task like {fragment!r} in {path}")


def test_both_owners_of_the_stack_dir_agree_on_its_mode():
    tier1 = _dir_task_mode(TASKS, "ensure the stack dir exists")
    infra = _dir_task_mode(INFRA_SWARM_STACK, "stack file dir")
    assert tier1 == infra, (
        f"roles/tier1_stack says {tier1} and roles/infrastructure says {infra} "
        "for /etc/catena/stacks. Each converge resets the other and both "
        "report changed, so a re-converge is never clean."
    )


def test_the_agreed_mode_is_the_tighter_one():
    """A compose body can carry a credential -- the reason the directory's
    first owner picked 0750."""
    assert _dir_task_mode(TASKS, "ensure the stack dir exists") == "0750"


def test_the_two_roles_target_the_same_path():
    """The test above is only meaningful if they are the same directory."""
    tier1_dir = _defaults()["tier1_stack_dir"]
    assert tier1_dir == "/etc/catena/stacks"
