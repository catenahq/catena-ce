"""Every port-22 ufw rule carries the shared `catena_ssh_ufw_comment`.

The reconciler (catena-admin payload/lanes/catena-public-ports.py) writes the
rules for the ssh fragment with the comment `<UFW_COMMENT_PREFIX>:ssh`.
bootstrap/roles/common and tasks/ufw_lockdown.yml add the same rules
themselves, and ufw treats a rule whose comment differs as an update rather
than a duplicate. With two comments, each writer rewrites the other's rule, so
every operator converge reports the public-SSH rule changed on a host whose 22
is open, and every Lockdown rerun reports its rules changed.

That the shared value IS the reconciler's comment is checked where both repos
are present: ops automation/test_bench/tests/test_ssh_rule_comment_matches_the_reconciler.py.

Run: uv run pytest tests/unit/test_ssh_ufw_rules_share_one_comment.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COMMON = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks"
VAR = "{{ catena_ssh_ufw_comment }}"


def _flatten(node) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out += _flatten(item)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out += _flatten(node[key])
    return out


def _port22_rule_comments() -> dict[str, object]:
    """Task name -> the comment it gives a ufw allow rule on port 22."""
    found: dict[str, object] = {}
    for path in (COMMON / "main.yml", COMMON / "ufw_lockdown.yml"):
        for task in _flatten(yaml.safe_load(path.read_text(encoding="utf-8"))):
            ufw = task.get("community.general.ufw") or {}
            if str(ufw.get("port")) == "22" and ufw.get("rule") == "allow":
                found[task["name"]] = ufw.get("comment")
            cmd = task.get("ansible.builtin.command")
            argv = (cmd.get("argv") or []) if isinstance(cmd, dict) else []
            if argv[:2] == ["ufw", "allow"] and "22" in argv:
                found[task["name"]] = (argv[argv.index("comment") + 1]
                                       if "comment" in argv else None)
    return found


def test_every_port22_allow_uses_the_shared_comment():
    found = _port22_rule_comments()
    # The public rule in common, the lockdown's reopen, its tailscale0 rule and
    # its Docker-bridge rule.
    assert len(found) >= 4, found
    wrong = {name: c for name, c in found.items() if c != VAR}
    assert not wrong, (
        f"these port-22 rules carry their own comment, so the reconciler and "
        f"the task rewrite each other's rule every run: {wrong}")
