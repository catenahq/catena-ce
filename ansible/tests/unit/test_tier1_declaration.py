"""roles/tier1_stack hands each spec to the catena-tier1 host engine.

The create/inspect/diff/update ladder lives in catena-admin
payload/engines/tier1; tasks/reconcile_one.yml dispatches it -- one renderer
for the argv instead of one per language, and an apply path that exists on
the host rather than only inside a converge.

What this file pins is the seam: the spec reaches the engine unaltered, the
converge reads the engine's own changed count, and a missing engine stops the
converge instead of being skipped.

Run: uv run pytest tests/unit/test_tier1_declaration.py
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "roles" / "tier1_stack"
TASKS = ROLE / "tasks" / "main.yml"
RECONCILE = ROLE / "tasks" / "reconcile_one.yml"
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


def _task(path: Path, fragment: str) -> dict:
    for task in _flatten(yaml.safe_load(path.read_text())):
        if fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"no task named like {fragment!r} in {path}")


# --- the dispatch -------------------------------------------------------


def test_the_engine_is_the_one_installed_by_the_payload_role():
    assert _defaults()["tier1_engine_bin"] == "/usr/local/bin/catena-tier1"


def test_the_spec_reaches_the_engine_on_stdin_unaltered():
    """No second rendering between the role and the apply. A transform here
    would be a description of the service free to drift from the one the same
    dict renders into the compose oracle."""
    task = _task(RECONCILE, "reconcile via catena-tier1")["ansible.builtin.command"]
    assert task["argv"][0] == "{{ tier1_engine_bin }}"
    assert task["argv"][1] == "apply"
    stdin = task["stdin"]
    assert "tier1_spec" in stdin
    assert "'services'" in stdin or '"services"' in stdin
    assert "to_json" in stdin


def test_the_declaration_shape_is_what_the_engine_decodes():
    """The engine's Decode takes {"services": [...]}; a bare list or a bare
    object would be rejected at the far end of a pipe, where the error says
    less than it does here."""
    template = _task(RECONCILE, "reconcile via catena-tier1")[
        "ansible.builtin.command"]["stdin"]
    # Stand in for the Jinja render: one spec, wrapped exactly as the template
    # wraps it.
    spec = {"name": "catena-traefik", "image": "traefik:v3.7.7"}
    assert "{'services': [tier1_spec]}" in template.replace('"', "'")
    decoded = json.loads(json.dumps({"services": [spec]}))
    assert list(decoded) == ["services"]
    assert isinstance(decoded["services"], list)


def test_changed_comes_from_the_engines_own_count():
    """The engine emits changed=<n> off Result.Changed(). Inferring it here
    from created/updated/converged would be a second definition of "did this
    run touch anything" living at the call site."""
    cond = str(_task(RECONCILE, "reconcile via catena-tier1")["changed_when"])
    assert "changed=0" in cond
    assert "not in" in cond


def test_a_missing_engine_stops_the_converge():
    """Unlike the Cloudflare tunnel, there is no deferral: without the engine
    there is no traefik, no postgres and no portainer, so a converge that
    carried on would report success over a host with no control plane."""
    task = _task(RECONCILE, "the reconcile engine is missing")
    assert "ansible.builtin.fail" in task
    cond = str(task["when"])
    assert "_t1_bin.stat.exists" in cond
    # NOT gated on catena_payload_engines_expected: a host whose payload is
    # staged out of band still cannot come up without the engine.
    assert "catena_payload_engines_expected" not in cond


def test_nothing_writes_a_declaration_file():
    """The spec arrives on stdin. A file would be a third copy of one
    description -- role vars, rendered oracle, file -- and the copy nothing
    reads on the run where it goes stale."""
    body = TASKS.read_text() + RECONCILE.read_text() + DEFAULTS.read_text()
    assert "tier1.json" not in body
    assert "tier1_declaration_path" not in body


# --- two rosters, two lifecycles ----------------------------------------


def test_the_control_plane_and_the_companions_render_to_different_files():
    d = _defaults()
    assert d["tier1_stack_path"] != d["tier1_companions_path"]
    # Both are unrendered Jinja here; what matters is that they are anchored to
    # the same directory, which is the one roles/infrastructure also owns.
    assert "tier1_stack_dir" in d["tier1_companions_path"]
    assert "tier1_stack_dir" in d["tier1_stack_path"]


def test_the_companion_roster_is_not_under_the_completeness_guard():
    """A companion is present exactly when its consumer is, so an accumulator
    holding fewer than last converge is the answer rather than a partial one.
    Requiring a roster would fail every host with no real-time app."""
    guarded = None
    for task in yaml.safe_load(TASKS.read_text()):
        if "block" not in task:
            continue
        names = [t.get("name") or "" for t in task["block"]]
        if any("companion file" in n for n in names):
            guarded = task
    assert guarded is not None, "the companion write is not inside a block"
    assert "tier1_stack_required" not in str(guarded["when"])


def test_the_control_plane_write_keeps_its_completeness_guard():
    """A tag-scoped converge accumulates a SUBSET, and a file naming two of
    three services looks like the whole plane while not being it."""
    guarded = None
    for task in yaml.safe_load(TASKS.read_text()):
        if "block" not in task:
            continue
        names = [t.get("name") or "" for t in task["block"]]
        if any("rendered control-plane file" in n for n in names):
            guarded = task
    assert guarded is not None
    cond = str(guarded["when"])
    assert "tier1_stack_required" in cond
    assert "difference(_tier1_present) | length == 0" in cond


def test_the_companion_file_is_removed_when_nothing_contributed():
    """Left behind, it would describe services this host no longer runs -- and
    the whole point of rendering is that the file matches reality."""
    task = _task(TASKS, "no companions on this host")
    assert task["ansible.builtin.file"]["state"] == "absent"
    assert "_tier1_companions | length == 0" in str(task["when"])


def test_coturn_contributes_to_the_companion_roster_not_the_control_plane():
    coturn = (ANSIBLE / "roles" / "coturn" / "tasks" / "deploy.yml").read_text()
    assert "catena_companion_specs" in coturn
    assert "catena_tier1_specs" not in coturn


# --- two roles, one directory ------------------------------------------------
#
# roles/infrastructure (swarm_stack.yml) creates /etc/catena/stacks for the
# app stack files; roles/tier1_stack creates it for the control-plane render.
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
    assert _defaults()["tier1_stack_dir"] == "/etc/catena/stacks"
