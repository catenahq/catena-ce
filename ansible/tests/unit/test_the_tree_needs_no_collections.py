"""Nothing in the Ansible tree names a Galaxy collection.

WHAT THIS IS FOR. The whole tree runs on the client host, from the copy the
catena-admin panel image carries. What travels is what helpers/tree_hash.py
lists: the files git tracks under ansible/, minus tests/. `.collections/` is
gitignored, so the galaxy tree does not travel, and the host runs ansible-core
alone -- no `ansible-galaxy` runs there.

So the host can run `ansible.builtin` and the plugins in this tree, and
nothing else. A task naming a collection module is a run that cannot happen,
on every host, and the failure does not wait for the condition that task is
guarded by:

    TASK [include the file that names the missing module] ***
    [ERROR]: couldn't resolve module/action 'community.general.ufw'.

Ansible resolves the action while PARSING the included file, before any
condition is evaluated, so a guard on the task protects nothing and an
unconditional include is enough to take the whole play down. A collection
filter, test or lookup fails the same way once the expression holding it is
templated.

WHY A STATIC GATE. `--syntax-check` does not reach a dynamic include, which is
not parsed until it runs. The only other thing that catches it is a run on a
host.

Covered: every playbook, every role on both sides (bootstrap/roles,
reconcile/roles) and the shared task files under playbooks/tasks.

Run: uv run pytest tests/unit/test_the_tree_needs_no_collections.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]

# The parts of the tree that hold Ansible content. tests/ does not travel and
# .collections/ is the galaxy tree itself; neither sits under these.
_ROOTS = ("bootstrap", "reconcile", "playbooks")

# What a host can resolve with ansible-core alone: the builtin set, under both
# of its names (`ansible.legacy` adds local library/ overrides).
_SHIPPED_NAMESPACES = ("ansible.builtin.", "ansible.legacy.")

# namespace.collection.module -- the shape every module in this tree is
# written in, because the repo is fully qualified throughout.
_FQCN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")

# A filter, test or lookup from a collection, as an expression names it. The
# lookbehind keeps a hostname or URL such as https://community.example.com
# from reading as one.
_COLLECTION_PLUGIN = re.compile(
    r"(?<![\w./:@-])(?:community\.[a-z0-9_]+|ansible\.posix|ansible\.utils)"
    r"\.[a-z0-9_]+")

# The Jinja in a template: what Ansible evaluates, as opposed to the text it
# copies through.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)

# Keys that structure a task rather than name a module. A task made only of
# these has no module to check -- `block`/`rescue`/`always` hold their own
# task lists, which _tasks_in recurses into.
_STRUCTURAL = {"block", "rescue", "always"}


def _yaml_files() -> list[Path]:
    out = sorted(p for root in _ROOTS for p in (_ANSIBLE / root).rglob("*.yml")
                 if p.is_file())
    assert out, "no YAML found under the tree; this gate would pass over nothing"
    return out


def _task_files() -> list[Path]:
    """Every file that holds TASKS: the roles' tasks/ and handlers/, the
    shared task files, and the playbooks themselves.

    defaults/, vars/, meta/ and group_vars/ are left out because they are
    variable maps -- a mapping there is a value, not a task with no module.
    Their expressions are still read, by the plugin check below.

    Discovered rather than listed, so a new role or task file is covered the
    day it lands."""
    playbooks = _ANSIBLE / "playbooks"
    return [p for p in _yaml_files()
            if {"tasks", "handlers"} & set(p.relative_to(_ANSIBLE).parts)
            or p.parent == playbooks]


def _templates() -> list[Path]:
    return sorted(p for root in _ROOTS for p in (_ANSIBLE / root).rglob("*.j2")
                  if p.is_file())


def _tasks_in(doc) -> list[dict]:
    """Flatten every task dict out of a loaded YAML document.

    Handles all three shapes one file can be: a play list (the playbook), a
    bare task list (a role's tasks/ file), and the nested lists inside
    block/rescue/always. A play (it names `hosts`) is never a task itself,
    including one that only lists roles."""
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
        if "hosts" not in node and (not nested or _STRUCTURAL & set(node)):
            found.append(node)

    walk(doc)
    return found


def _strings_in(node):
    """Every string value in a loaded YAML document. Ansible templates each
    one, so each is an expression the host evaluates."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _strings_in(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings_in(value)


def _load(path: Path):
    """Strict: a file this cannot parse is a file the gate cannot vouch for."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _module_keys(task: dict) -> list[str]:
    """The fully-qualified keys of a task, module or not.

    Checking the SHAPE rather than maintaining a list of Ansible's task
    keywords: no keyword is fully qualified, so every FQCN key in a task is a
    module name and the list stays right without being updated."""
    return [k for k in task if isinstance(k, str) and _FQCN.match(k)]


def _plugins_named(text: str) -> list[str]:
    return [m.group(0) for m in _COLLECTION_PLUGIN.finditer(text)]


def test_no_task_names_a_collection_module():
    """The defect itself: a module the host does not carry."""
    offenders = []
    for path in _task_files():
        for task in _tasks_in(_load(path)):
            for key in _module_keys(task):
                if not key.startswith(_SHIPPED_NAMESPACES):
                    offenders.append(
                        f"{path.relative_to(_ANSIBLE)}: {key}"
                        f" (task: {task.get('name', '<unnamed>')})")
    assert not offenders, (
        "these tasks name modules ansible-core does not carry, so a run on the "
        "host fails while parsing them -- whatever they are guarded by:\n  "
        + "\n  ".join(offenders))


def test_every_task_qualifies_its_module():
    """A bare module name is the same defect wearing a different hat.

    `ufw:` resolves through the collections search path exactly as
    `community.general.ufw:` does, so it fails on the host in the same way
    and reads as builtin to whoever is scanning the file. Requiring the
    namespace is what makes the check above complete: every task has a
    qualified module, so a task with no builtin key has a module that is
    neither builtin nor declared."""
    offenders = []
    for path in _task_files():
        for task in _tasks_in(_load(path)):
            if _STRUCTURAL & set(task):
                continue
            if not _module_keys(task):
                offenders.append(
                    f"{path.relative_to(_ANSIBLE)}: "
                    f"{task.get('name', '<unnamed>')}")
    assert not offenders, (
        "these tasks name no fully-qualified module; an unqualified name "
        "resolves through the collections path the host does not have:\n  "
        + "\n  ".join(offenders))


def test_no_expression_names_a_collection_plugin():
    """A collection filter, test or lookup, in any YAML value or in a
    template's Jinja, fails the run that templates it."""
    offenders = []
    for path in _yaml_files():
        for text in _strings_in(_load(path)):
            offenders += [f"{path.relative_to(_ANSIBLE)}: {name}"
                          for name in _plugins_named(text)]
    for path in _templates():
        body = path.read_text(encoding="utf-8", errors="ignore")
        for span in _JINJA.findall(body):
            offenders += [f"{path.relative_to(_ANSIBLE)}: {name}"
                          for name in _plugins_named(span)]
    assert not offenders, (
        "these expressions name a collection plugin ansible-core does not "
        "carry:\n  " + "\n  ".join(offenders))


def test_the_gate_reads_real_files():
    """A gate that walked an empty set would pass for the wrong reason."""
    files = _task_files()
    assert len(files) > 100, f"only {len(files)} task-bearing YAML files found"
    tasks = sum(len(_tasks_in(_load(p))) for p in files)
    assert tasks > 1000, f"only {tasks} tasks found across {len(files)} files"
    assert len(_templates()) > 50, f"only {len(_templates())} templates found"


def test_the_gate_reads_both_sides_and_every_playbook():
    """Bootstrap-side roles run on the host as much as reconcile-side ones.
    The files that last named a collection module are named here, so a
    discovery rule that drops a side fails by name."""
    rel = {str(p.relative_to(_ANSIBLE)) for p in _task_files()}
    for path in ("playbooks/bootstrap.yml",
                 "playbooks/converge.yml",
                 "playbooks/lockdown.yml",
                 "playbooks/tasks/converge_tail.yml",
                 "bootstrap/roles/common/tasks/main.yml",
                 "bootstrap/roles/common/tasks/ufw_lockdown.yml",
                 "bootstrap/roles/storage/tasks/bulk.yml",
                 "bootstrap/roles/catena_admin_host/tasks/main.yml",
                 "reconcile/roles/catena-admin/tasks/main.yml"):
        assert path in rel, f"{path} is not in the gated set"
    assert not any(p.startswith(("tests/", ".collections/")) for p in rel), rel


def test_the_gate_catches_a_collection_module():
    """The detector, run against the shape that reaches a host -- a collection
    module, guarded by a condition, inside an included task list -- and its
    nearest legitimate neighbours, so the gate is shown to separate them."""
    guarded = yaml.safe_load("""
    - name: "RC Jitsi: open JVB media UDP (10000) when RC is deployed"
      community.general.ufw:
        rule: allow
        port: 10000
        proto: udp
      when: _rc_jitsi_svc.stdout | trim | length > 0
    """)
    tasks = _tasks_in(guarded)
    assert len(tasks) == 1
    keys = _module_keys(tasks[0])
    assert keys == ["community.general.ufw"], keys
    assert not keys[0].startswith(_SHIPPED_NAMESPACES), (
        "the namespace check passes a collection module")

    unqualified = yaml.safe_load("""
    - name: the same defect without the namespace
      ufw:
        rule: allow
    """)
    assert not _module_keys(_tasks_in(unqualified)[0]), (
        "a bare module name has to read as unqualified, or the second test "
        "passes everything")

    allowed = yaml.safe_load("""
    - name: a builtin task
      ansible.builtin.copy:
        dest: /tmp/x
        content: y
      when: false
    - name: the same set under its other name
      ansible.legacy.command:
        cmd: /bin/true
    """)
    for task in _tasks_in(allowed):
        keys = _module_keys(task)
        assert len(keys) == 1 and keys[0].startswith(_SHIPPED_NAMESPACES), keys


def test_the_gate_catches_a_collection_plugin():
    """Filters, tests and lookups are caught; attribute chains, URLs and
    builtin filters are not."""
    doc = yaml.safe_load("""
    - name: a collection filter, test and lookup
      ansible.builtin.debug:
        msg: "{{ data | community.general.json_query('a') }}"
      when: addr is ansible.utils.ipv4
      vars:
        secret: "{{ lookup('community.general.passwordstore', 'x') }}"
        mounts: "{{ x | ansible.posix.json }}"
    """)
    found = [n for s in _strings_in(doc) for n in _plugins_named(s)]
    assert found == ["community.general.json_query", "ansible.utils.ipv4",
                     "community.general.passwordstore", "ansible.posix.json"], found

    clean = yaml.safe_load("""
    - name: no collection here
      ansible.builtin.debug:
        msg: "{{ _ops.ansible_facts.getent_passwd.ops | ansible.builtin.to_json }}"
      vars:
        forum: https://community.example.com/t/x
        mail: admin@community.example.com
    """)
    assert not [n for s in _strings_in(clean) for n in _plugins_named(s)]

    template = "plain community.general.ufw text {{ x | community.general.y }}"
    spans = _JINJA.findall(template)
    assert [n for s in spans for n in _plugins_named(s)] == ["community.general.y"]


def test_the_gate_sees_inside_a_block():
    """Half the tree's tasks are inside block/, and a walker that stopped at
    the wrapper would report a clean tree while skipping them."""
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

    If `.collections/` ever becomes tracked, the tests above enforce a
    restriction that has stopped applying -- and the honest response is to
    delete them, not to keep obeying them. This is what says so."""
    ignore = (_ANSIBLE / ".gitignore").read_text(encoding="utf-8")
    assert ".collections/" in ignore, (
        "ansible/.gitignore no longer ignores .collections/. If the galaxy "
        "tree is now tracked it travels in the panel image, and the "
        "collection restriction these tests enforce is obsolete.")
