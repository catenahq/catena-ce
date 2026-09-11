"""Every module a reconcile-side task names has to be in the image.

WHAT THIS IS FOR. The reconcile tree travels to a client host inside the
catena-admin panel image, and `scripts/vendor-catena-ce.sh` fills that image
from `git ls-files -- ansible`. `.collections/` is gitignored, so the galaxy
tree is not tracked, so it does not travel. On the host, catena-converge
installs `ansible-core` and nothing else -- no `ansible-galaxy` runs there.

So the host can run `ansible.builtin` and the plugins in this tree, and
nothing else. A reconcile-side task naming a collection module is a converge
that cannot happen, on every host, and the failure does not wait for the
condition that task is guarded by:

    TASK [include the file that names the missing module] ***
    [ERROR]: couldn't resolve module/action 'community.general.ufw'.

That is `when: false`, under `include_tasks`, measured. Ansible resolves the
action while PARSING the included file, before any condition is evaluated, so
a guard on the task protects nothing and an unconditional include is enough to
take the whole play down. One such task shipped -- the Rocket.Chat JVB ufw
rule -- and it would have failed the infrastructure role on every host.

WHY A STATIC GATE. `--syntax-check` does not reach it either: a dynamic
include is not parsed until it runs, which is the same reason the defect
survived. The only two things that would have caught it are this and an
on-host converge.

The bootstrap side is deliberately out of scope. An operator runs it from a
controller that has the collections installed, which is what
`ansible-galaxy collection install -r requirements.yml` is for.

Run: uv run pytest tests/unit/test_reconcile_needs_no_collections.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]

# What a host can resolve with ansible-core alone. `ansible.legacy` is the
# other name for the same set plus local library/ overrides, and nothing here
# uses it; it is named so that adding it is a decision rather than a typo that
# happens to pass.
_SHIPPED_NAMESPACES = ("ansible.builtin.",)

# namespace.collection.module -- the shape every collection module in this
# tree is written in, because the repo is fully qualified throughout.
_FQCN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")

# Keys that structure a task rather than name a module. A task made only of
# these has no module to check -- `block`/`rescue`/`always` hold their own
# task lists, which _tasks_in recurses into.
_STRUCTURAL = {"block", "rescue", "always"}


def _boundary() -> dict:
    return yaml.safe_load((_ANSIBLE / "boundary.yml").read_text(encoding="utf-8"))


def _operator_only() -> set[str]:
    """Files boundary.yml says a converge never reaches.

    Read rather than restated. The declaration of what is outside a converge
    already exists, and a second copy here is a second thing to keep true."""
    return set(_boundary().get("operator_only_files") or [])


def _reconcile_files() -> list[Path]:
    """Every file a converge can reach that holds TASKS: the reconcile roles'
    tasks/ and handlers/, the shared task files, and the playbook itself.

    defaults/, vars/ and meta/ are excluded because they are variable maps --
    a mapping there is a value, not a task with no module.

    Discovered rather than listed. A new role directory or a new shared task
    file is covered the day it lands, which is the only way a gate about "what
    ships" stays true across a move."""
    out = [p for p in (_ANSIBLE / "reconcile").rglob("*.yml")
           if p.is_file() and {"tasks", "handlers"} & set(p.parts)]
    out += [p for p in (_ANSIBLE / "playbooks" / "tasks").rglob("*.yml")
            if p.is_file()]
    out.append(_ANSIBLE / "playbooks" / "reconcile.yml")

    skip = _operator_only()
    kept = [p for p in out if str(p.relative_to(_ANSIBLE)) not in skip]
    assert kept, "no reconcile-side tasks found; this gate would pass over nothing"
    return kept


def _tasks_in(doc) -> list[dict]:
    """Flatten every task dict out of a loaded YAML document.

    Handles all three shapes one file can be: a play list (the playbook), a
    bare task list (a role's tasks/ file), and the nested lists inside
    block/rescue/always."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        # A play carries its tasks under these keys; a task carries nested
        # tasks under the structural ones. Same recursion either way.
        nested = False
        for key in ("pre_tasks", "tasks", "post_tasks", "handlers",
                    "block", "rescue", "always"):
            if key in node:
                nested = True
                walk(node[key])
        if not nested or _STRUCTURAL & set(node):
            found.append(node)

    walk(doc)
    return found


def _load(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
    except yaml.YAMLError:
        # Not this gate's question. tests/unit/test_yaml_parses.py owns it,
        # and failing here too would report one defect as two.
        return None


def _module_keys(task: dict) -> list[str]:
    """The fully-qualified keys of a task, module or not.

    Checking the SHAPE rather than maintaining a list of Ansible's task
    keywords: no keyword is fully qualified, so every FQCN key in a task is a
    module name and the list stays right without being updated."""
    return [k for k in task if isinstance(k, str) and _FQCN.match(k)]


def test_no_reconcile_task_names_a_collection_module():
    """The defect itself: a module the image does not carry."""
    offenders = []
    for path in _reconcile_files():
        doc = _load(path)
        if doc is None:
            continue
        for task in _tasks_in(doc):
            for key in _module_keys(task):
                if not key.startswith(_SHIPPED_NAMESPACES):
                    offenders.append(
                        f"{path.relative_to(_ANSIBLE)}: {key}"
                        f" (task: {task.get('name', '<unnamed>')})")
    assert not offenders, (
        "these reconcile-side tasks name modules the panel image does not "
        "carry, so an on-host converge fails while parsing them -- whatever "
        "they are guarded by:\n  " + "\n  ".join(offenders))


def test_every_reconcile_task_qualifies_its_module():
    """A bare module name is the same defect wearing a different hat.

    `ufw:` resolves through the collections search path exactly as
    `community.general.ufw:` does, so it fails on the host in the same way
    and reads as builtin to whoever is scanning the file. Requiring the
    namespace is what makes the check above complete: every task has a
    qualified module, so a task with no ansible.builtin key has a module that
    is neither builtin nor declared."""
    offenders = []
    for path in _reconcile_files():
        doc = _load(path)
        if doc is None:
            continue
        for task in _tasks_in(doc):
            if _STRUCTURAL & set(task):
                continue
            if not _module_keys(task):
                offenders.append(
                    f"{path.relative_to(_ANSIBLE)}: "
                    f"{task.get('name', '<unnamed>')}")
    assert not offenders, (
        "these reconcile-side tasks name no fully-qualified module; an "
        "unqualified name resolves through the collections path the host "
        "does not have:\n  " + "\n  ".join(offenders))


def test_the_gate_reads_real_files():
    """A gate that walked an empty set would pass for the wrong reason."""
    files = _reconcile_files()
    assert len(files) > 40, f"only {len(files)} reconcile-side YAML files found"
    tasks = sum(len(_tasks_in(_load(p))) for p in files if _load(p) is not None)
    assert tasks > 200, f"only {tasks} tasks found across {len(files)} files"


def test_the_gate_catches_the_defect_it_was_written_for():
    """The detector, run against the task that shipped.

    Both tests above pass on the current tree, and a detector that passes is
    indistinguishable from one that looks at nothing. This feeds them the
    shape that was actually there -- a collection module, guarded by a
    condition, inside an included task list -- and the nearest legitimate
    neighbour, so the gate is shown to separate them."""
    shipped = yaml.safe_load("""
    - name: "RC Jitsi: open JVB media UDP (10000) when RC is deployed"
      community.general.ufw:
        rule: allow
        port: 10000
        proto: udp
      when: _rc_jitsi_svc.stdout | trim | length > 0
    """)
    tasks = _tasks_in(shipped)
    assert len(tasks) == 1
    keys = _module_keys(tasks[0])
    assert keys == ["community.general.ufw"], keys
    assert not keys[0].startswith(_SHIPPED_NAMESPACES), (
        "the namespace check would have passed the module that broke the "
        "converge")

    unqualified = yaml.safe_load("""
    - name: the same defect without the namespace
      ufw:
        rule: allow
    """)
    assert not _module_keys(_tasks_in(unqualified)[0]), (
        "a bare module name has to read as unqualified, or the second test "
        "passes everything")

    allowed = yaml.safe_load("""
    - name: a task that ships in the image
      ansible.builtin.copy:
        dest: /tmp/x
        content: y
      when: false
    """)
    keys = _module_keys(_tasks_in(allowed)[0])
    assert keys == ["ansible.builtin.copy"], keys
    assert keys[0].startswith(_SHIPPED_NAMESPACES)


def test_the_gate_sees_inside_a_block():
    """Half the reconcile tree's tasks are inside block/, and a walker that
    stopped at the wrapper would report a clean tree while skipping them."""
    doc = yaml.safe_load("""
    - name: wrapper
      when: something
      block:
        - name: buried
          community.general.ufw:
            rule: allow
    """)
    names = [t.get("name") for t in _tasks_in(doc)]
    assert "buried" in names, names
    buried = next(t for t in _tasks_in(doc) if t.get("name") == "buried")
    assert _module_keys(buried) == ["community.general.ufw"]


def test_the_vendored_tree_cannot_carry_collections():
    """The premise, asserted rather than remembered.

    If `.collections/` ever became tracked, the two tests above would be
    enforcing a restriction that no longer applies -- and the honest response
    would be to delete them, not to keep obeying them. This is what would say
    so."""
    ignore = (_ANSIBLE / ".gitignore").read_text(encoding="utf-8")
    assert ".collections/" in ignore, (
        "ansible/.gitignore no longer ignores .collections/. If the galaxy "
        "tree is now tracked it travels in the panel image, and the "
        "collection restriction these tests enforce is obsolete.")
