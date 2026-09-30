"""The reconcile tree reads no variable that only a bootstrap role defines.

A host converges itself with playbooks/reconcile.yml alone (catena-admin
catena-converge), which runs no bootstrap role, so a bootstrap default is
undefined there. From a laptop, converge.yml runs both halves in one play and
the same read resolves, which is how a gap here stays invisible until a host
converges on its own and stops at the first task that reads it.

A value both halves need belongs in playbooks/group_vars/all/main.yml.

The scan is textual: names inside {{ }} / {% %} and in when/until/that
expressions, against top-level keys of defaults, vars and group_vars and the
names set_fact, register, loop_var and vars: blocks introduce.

Run: uv run pytest tests/unit/test_reconcile_reads_no_bootstrap_default.py
"""
from __future__ import annotations

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2]

_KEY = re.compile(r"^([a-z_][a-z0-9_]*):", re.M)
_INDENTED_KEY = re.compile(r"^\s+([a-z_][a-z0-9_]*):", re.M)
_REGISTER = re.compile(r"^\s*register:\s*([a-z_][a-z0-9_]*)", re.M)
_LOOP_VAR = re.compile(r"loop_var:\s*([a-z_][a-z0-9_]*)")
_JINJA = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)
_CONDITION = re.compile(
    r"^\s*(?:-\s*)?(?:when|until|failed_when|changed_when|that):\s*(.*)$", re.M)
_IDENT = re.compile(r"\b([a-z_][a-z0-9_]*)\b")


def _top_keys(paths) -> set[str]:
    out: set[str] = set()
    for p in paths:
        out |= set(_KEY.findall(p.read_text(errors="replace")))
    return out


# `file: tasks/x.yml` under an include, or a one-line
# `import_tasks: "{{ playbook_dir }}/tasks/x.yml"` from a role.
_INCLUDED_FILE = re.compile(
    r"""^\s*(?:-\s*)?(?:file|(?:ansible\.builtin\.)?import_tasks):\s*["']?"""
    r"""(?:\{\{\s*playbook_dir\s*\}\}/)?(tasks/[^"'\s]+\.yml)["']?\s*$""",
    re.M)


def _reconcile_files() -> list[Path]:
    """The reconcile roles, reconcile.yml, and every playbooks/tasks file
    either includes, followed through their own includes."""
    files = [p for p in (ANSIBLE / "reconcile").rglob("*") if p.is_file()]
    queue = [ANSIBLE / "playbooks" / "reconcile.yml", *files]
    seen: set[Path] = set()
    while queue:
        p = queue.pop()
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        queue += [ANSIBLE / "playbooks" / rel
                  for rel in _INCLUDED_FILE.findall(p.read_text(errors="replace"))]
    return sorted(seen | set(files))


def _defined_for_reconcile(files) -> set[str]:
    names = _top_keys(
        list(ANSIBLE.glob("reconcile/roles/*/defaults/*.yml"))
        + list(ANSIBLE.glob("reconcile/roles/*/vars/*.yml"))
        + list(ANSIBLE.glob("playbooks/group_vars/all/*.yml")))
    for p in files:
        text = p.read_text(errors="replace")
        names |= set(_REGISTER.findall(text)) | set(_LOOP_VAR.findall(text))
        for block in re.finditer(r"set_fact:\s*\n((?:\s{4,}.*\n)+)", text):
            names |= set(_INDENTED_KEY.findall(block.group(1)))
        for block in re.finditer(r"^\s*vars:\s*\n((?:\s{2,}.*\n)+)", text, re.M):
            names |= set(_INDENTED_KEY.findall(block.group(1)))
    return names


def _read_by_reconcile(files) -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for p in files:
        text = p.read_text(errors="replace")
        exprs = [a or b for a, b in _JINJA.findall(text)] + _CONDITION.findall(text)
        for expr in exprs:
            for name in _IDENT.findall(expr):
                reads.setdefault(name, set()).add(str(p.relative_to(ANSIBLE)))
    return reads


def test_every_value_the_reconcile_reads_resolves_without_a_bootstrap_role():
    files = _reconcile_files()
    bootstrap_only = _top_keys(
        list(ANSIBLE.glob("bootstrap/roles/*/defaults/*.yml"))
        + list(ANSIBLE.glob("bootstrap/roles/*/vars/*.yml"))
    ) - _defined_for_reconcile(files)
    reads = _read_by_reconcile(files)
    offenders = {name: sorted(reads[name]) for name in bootstrap_only if name in reads}
    assert not offenders, (
        "the reconcile tree reads variables only a bootstrap role defines; a "
        "host converging itself (reconcile.yml alone) has them undefined. Move "
        "each to playbooks/group_vars/all/main.yml:\n"
        + "\n".join(f"  {n} <- {', '.join(f)}" for n, f in sorted(offenders.items()))
    )


def test_the_scan_sees_the_shared_values():
    """A scan that stopped reading the reconcile tree would pass anything."""
    reads = _read_by_reconcile(_reconcile_files())
    for name in ("catena_admin_service_name", "catena_admin_image", "swarm_stack_dir"):
        assert name in reads, f"the scan no longer sees {name} being read"


def test_the_scan_follows_a_roles_import_of_a_shared_task_file():
    """reconcile/roles/host_maintenance imports playbooks/tasks/apt_settings.yml,
    whose reads resolve on the host only if the scan reaches them."""
    files = {str(p.relative_to(ANSIBLE)) for p in _reconcile_files()}
    assert "playbooks/tasks/apt_settings.yml" in files
    assert "catena_apt_proxy_url" in _read_by_reconcile(_reconcile_files())
