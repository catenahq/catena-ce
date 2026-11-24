"""A role's validate tasks read no variable that only another role defines.

playbooks/validate.yml runs each role's tasks/validate.yml through
include_role, which keeps a role's defaults and vars to that role. The converge
lists its roles in one play, where every role's defaults are visible to every
other, so a read of another role's default resolves there and stops the install
at its last step, the validate play, with "is undefined".

A value two roles read belongs in playbooks/group_vars/all/main.yml.

The scan follows each validate file through its own includes and the role tasks
it includes (include_role with tasks_from), each file against the defaults and
vars of the role it belongs to. The names the validate play's tasks introduce
(set_fact, register, loop_var, vars: blocks) count as defined, since a fact one
role sets stays set for the roles after it.

Run: uv run pytest tests/unit/test_validate_reads_no_other_roles_default.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ansible_tree import PLAYBOOKS, ROLE_ROOTS, role_dir, walk_tasks
from test_reconcile_reads_no_bootstrap_default import _CONDITION, _IDENT, _JINJA, _introduced, _top_keys

VALIDATE = PLAYBOOKS / "validate.yml"

# include_tasks / import_tasks of a file in the same role's tasks/.
_ROLE_INCLUDE = re.compile(
    r"""^\s*(?:-\s*)?(?:file|(?:ansible\.builtin\.)?(?:include|import)_tasks):\s*["']?"""
    r"""([\w.-]+\.yml)["']?\s*$""",
    re.M)
# What resolves without the name being defined: an attribute of another value
# (an include_vars namespace, a registered result) and a read with a default.
_NOT_A_BARE_READ = re.compile(r"\.\s*[a-z_]\w*|\b[a-z_]\w*\s*\|\s*default\b")
_COMMENT = re.compile(r"^\s*#.*$", re.M)
_ROLE_INCLUDE_KEYS = ("ansible.builtin.include_role", "ansible.builtin.import_role",
                      "include_role", "import_role")


def _validate_roles() -> list[str]:
    return yaml.safe_load(VALIDATE.read_text())[0]["vars"]["validate_roles"]


def _own(role: Path) -> set[str]:
    return _top_keys(list(role.glob("defaults/*.yml")) + list(role.glob("vars/*.yml")))


def _validate_files() -> dict[Path, Path]:
    """Each file the validate play runs from a role, mapped to its role."""
    queue = [(role_dir(r), role_dir(r) / "tasks" / "validate.yml") for r in _validate_roles()]
    seen: dict[Path, Path] = {}
    while queue:
        role, path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen[path] = role
        text = path.read_text(errors="replace")
        queue += [(role, role / "tasks" / name) for name in _ROLE_INCLUDE.findall(text)]
        for task in walk_tasks(yaml.safe_load(text) or []):
            include = next((task[k] for k in _ROLE_INCLUDE_KEYS if k in task), None)
            if isinstance(include, dict) and "{{" not in str(include.get("name", "{{")):
                other = role_dir(include["name"])
                queue.append((other, other / "tasks" / f"{include.get('tasks_from', 'main')}.yml"))
    return seen


def _play_files() -> list[Path]:
    """The validate playbook and the playbooks/tasks files its pre_tasks include."""
    play = yaml.safe_load(VALIDATE.read_text())[0]
    files = [VALIDATE]
    for task in play.get("pre_tasks") or []:
        include = task.get("ansible.builtin.include_tasks")
        if include:
            files.append(PLAYBOOKS / (include if isinstance(include, str) else include["file"]))
    return files


def _reads(path: Path) -> set[str]:
    """The names a task file reads that must be defined, comments left out."""
    text = _COMMENT.sub("", path.read_text(errors="replace"))
    exprs = [a or b for a, b in _JINJA.findall(text)] + _CONDITION.findall(text)
    return {name for expr in exprs for name in _IDENT.findall(_NOT_A_BARE_READ.sub(" ", expr))}


def _role_defined() -> set[str]:
    names: set[str] = set()
    for root in ROLE_ROOTS:
        names |= _top_keys(list(root.glob("*/defaults/*.yml")) + list(root.glob("*/vars/*.yml")))
    return names


def _shared(files: dict[Path, Path]) -> set[str]:
    return (_top_keys(list(PLAYBOOKS.glob("group_vars/all/*.yml")))
            | _introduced([*files, *_play_files()]))


def _offenders(files: dict[Path, Path], role_defined: set[str], shared: set[str]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for path, role in files.items():
        foreign = role_defined - shared - _own(role)
        for name in _reads(path) & foreign:
            out.setdefault(name, set()).add(str(path.relative_to(PLAYBOOKS.parent)))
    return {name: sorted(where) for name, where in out.items()}


def test_every_value_a_validate_reads_resolves_in_the_validate_play():
    files = _validate_files()
    offenders = _offenders(files, _role_defined(), _shared(files))
    assert not offenders, (
        "a role's validate tasks read variables only another role defines; "
        "playbooks/validate.yml includes each role on its own, so they are "
        "undefined there. Move each to playbooks/group_vars/all/main.yml:\n"
        + "\n".join(f"  {n} <- {', '.join(f)}" for n, f in sorted(offenders.items()))
    )


def test_the_scan_sees_the_validate_files_and_their_includes():
    """A scan that stopped following the play would pass anything."""
    files = {str(p.relative_to(PLAYBOOKS.parent)) for p in _validate_files()}
    assert "reconcile/roles/oauth2_proxy/tasks/validate.yml" in files
    assert "reconcile/roles/dispatch_path/tasks/_run_helper.yml" in files
    assert "bootstrap/roles/common/tasks/_assert_path_perms.yml" in files


def test_the_scan_flags_a_read_of_another_roles_default():
    """oauth2_proxy's validate lists the route files in traefik_dynamic_dir:
    were that directory a default of the traefik role alone, the scan reports
    the read."""
    files = _validate_files()
    offenders = _offenders(files, _role_defined() | {"traefik_dynamic_dir"},
                           _shared(files) - {"traefik_dynamic_dir"})
    assert "reconcile/roles/oauth2_proxy/tasks/validate.yml" in offenders["traefik_dynamic_dir"]
