"""No command argv item is a YAML boolean.

Ansible loads task files as YAML 1.1, where a bare on, off, yes or no is a
boolean. Inside an argv list that reaches the command as the string "True" or
"False": `[ufw, allow, in, on, tailscale0, ...]` runs `ufw allow in True
tailscale0` and ufw refuses the token. The file parses, the syntax check
passes, and the task fails only when it runs.

Run: uv run pytest tests/unit/test_command_argv_holds_no_yaml_boolean.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROOTS = ("playbooks", "bootstrap", "reconcile")


def _task_files() -> list[Path]:
    return [p for root in _ROOTS for p in (_ANSIBLE / root).rglob("*.yml")
            if p.is_file()]


def _argvs(node):
    """Every argv list under an ansible.builtin.command anywhere in node."""
    if isinstance(node, list):
        for item in node:
            yield from _argvs(item)
    elif isinstance(node, dict):
        cmd = node.get("ansible.builtin.command")
        if isinstance(cmd, dict) and isinstance(cmd.get("argv"), list):
            yield cmd["argv"]
        for value in node.values():
            yield from _argvs(value)


def _offenders(doc, where: str) -> list[str]:
    return [f"{where}: {argv}" for argv in _argvs(doc)
            if any(isinstance(item, bool) for item in argv)]


def test_no_argv_item_is_a_boolean() -> None:
    offenders = []
    for path in _task_files():
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
        except yaml.YAMLError:
            continue
        offenders += _offenders(doc, str(path.relative_to(_ANSIBLE)))
    assert not offenders, (
        "these argv lists hold a YAML boolean; quote the word:\n  "
        + "\n  ".join(offenders))


def test_the_check_catches_a_bare_on() -> None:
    doc = yaml.safe_load("""
    - name: shipped once
      ansible.builtin.command:
        argv: [ufw, allow, in, on, tailscale0]
    """)
    assert _offenders(doc, "x"), "a bare on must read as a boolean"
    quoted = yaml.safe_load("""
    - ansible.builtin.command:
        argv: [ufw, allow, in, "on", tailscale0]
    """)
    assert not _offenders(quoted, "x")


def test_the_check_reads_real_files() -> None:
    count = 0
    for path in _task_files():
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
        except yaml.YAMLError:
            continue
        count += sum(1 for _ in _argvs(doc))
    assert count > 50, f"only {count} command argv lists found"
