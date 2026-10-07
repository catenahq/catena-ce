"""The reconcile tree reads no variable that only a bootstrap role defines, and
none that only the install's inventory supplies.

A host converges itself with playbooks/reconcile.yml alone (catena-admin
catena-converge), which runs no bootstrap role, so a bootstrap default is
undefined there. The install runs converge.yml, both halves in one play, and
the same read resolves, which is how a gap here stays invisible until a host
converges on its own and stops at the first task that reads it.

A value both halves need belongs in playbooks/group_vars/all/main.yml.

The inventory is the same trap with a quieter failure. The host's own converge
has no installer `.env` and not the install's host vars, so a value read from
either resolves to something else there, and the two paths render different
files for one machine: each converge then undoes the other's, and the panel
rolls on the first converge after a switch of path.

The scan is textual: names inside {{ }} / {% %} and in when/until/that
expressions, against top-level keys of defaults, vars and group_vars and the
names set_fact, register, loop_var and vars: blocks introduce.

Run: uv run pytest tests/unit/test_reconcile_reads_no_bootstrap_default.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

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


_GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"

# Inventory values the host keeps a record of, so its own converge reads the
# same answer. Each carries where the record is written.
_RECORDED_ON_THE_HOST = {
    "public_ip": "bootstrap/roles/common writes catena_host_facts_path",
}


def _operator_supplied() -> set[str]:
    """Every host var the install's run inventory sets (install-host.sh writes
    it), and every shared value group_vars reads from the installer's .env."""
    script = (ANSIBLE / "install-host.sh").read_text()
    inventory = re.search(r'hosts\.yml" <<YAML\n(.*?)\nYAML\n', script, re.S).group(1)
    names = set(re.findall(r"^ {6}([a-z_]+):", inventory, re.M))
    shared = yaml.safe_load(_GROUP_VARS.read_text())
    names |= {k for k, v in shared.items() if "lookup('dotenv'" in str(v)}
    return names


def test_the_reconcile_reads_nothing_only_the_operator_supplies():
    reads = _read_by_reconcile(_reconcile_files())
    offenders = {
        name: sorted(reads[name])
        for name in _operator_supplied() - set(_RECORDED_ON_THE_HOST)
        if name in reads
    }
    assert not offenders, (
        "the reconcile tree reads values only the operator's inventory "
        "supplies; a host converging itself resolves them differently, so the "
        "two converge paths render different files for one machine:\n"
        + "\n".join(f"  {n} <- {', '.join(f)}" for n, f in sorted(offenders.items()))
        + "\nRead a stable on-host value instead (inventory_hostname, the "
          "store), or record the inventory's value on the host and list it in "
          "_RECORDED_ON_THE_HOST."
    )


def test_the_operator_supplied_scan_sees_the_inventory():
    """A scan that found no operator-only names would pass anything."""
    names = _operator_supplied()
    for name in ("ansible_python_interpreter", "public_ip", "ops_user"):
        assert name in names, f"the scan no longer sees {name} as operator-supplied"


def test_a_recorded_value_is_read_back_from_the_record():
    """public_ip is read by the reconcile only because the host carries the
    operator's value: common writes it where fact gathering reads it, and the
    shared definition prefers it over the default route."""
    shared = yaml.safe_load(_GROUP_VARS.read_text())
    assert "ansible_local.catena.public_ip" in shared["public_ip"]
    assert shared["catena_host_facts_path"].endswith("/catena.fact")
    common = (ANSIBLE / "bootstrap/roles/common/tasks/main.yml").read_text()
    assert "dest: \"{{ catena_host_facts_path }}\"" in common
    assert "'public_ip': public_ip" in common


def test_the_scan_follows_a_roles_import_of_a_shared_task_file():
    """reconcile/roles/host_maintenance imports playbooks/tasks/apt_settings.yml,
    whose reads resolve on the host only if the scan reaches them."""
    files = {str(p.relative_to(ANSIBLE)) for p in _reconcile_files()}
    assert "playbooks/tasks/apt_settings.yml" in files
    assert "catena_apt_proxy_url" in _read_by_reconcile(_reconcile_files())
