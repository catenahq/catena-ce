"""Every port-22 ufw rule carries the port reconciler's comment.

scripts/catena-public-ports.py writes the rules for the ssh fragment with the
comment `<UFW_COMMENT_PREFIX>:ssh`. bootstrap/roles/common and
tasks/ufw_lockdown.yml add the same rules themselves, and ufw treats a rule
whose comment differs as an update rather than a duplicate. With two comments,
each writer rewrites the other's rule, so every operator converge reports the
public-SSH rule changed on a host whose 22 is open, and every Lockdown rerun
reports its rules changed.

Run: uv run pytest tests/unit/test_ssh_ufw_rules_share_one_comment.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
COMMON = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks"
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"
SCRIPT = ANSIBLE / "scripts" / "catena-public-ports.py"
VAR = "{{ catena_ssh_ufw_comment }}"

sys.path.insert(0, str(ANSIBLE / "helpers"))
import public_ports as pp  # noqa: E402


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


def _reconciler():
    spec = importlib.util.spec_from_file_location("catena_public_ports", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_the_shared_comment_is_the_reconcilers():
    declared = yaml.safe_load(GROUP_VARS.read_text(encoding="utf-8"))
    want = f"{_reconciler().UFW_COMMENT_PREFIX}:{pp.SSH_OWNER}"
    assert declared["catena_ssh_ufw_comment"] == want


def test_every_port22_allow_uses_the_shared_comment():
    found = _port22_rule_comments()
    # The public rule in common, the lockdown's reopen, its tailscale0 rule and
    # its Docker-bridge rule.
    assert len(found) >= 4, found
    wrong = {name: c for name, c in found.items() if c != VAR}
    assert not wrong, (
        f"these port-22 rules carry their own comment, so the reconciler and "
        f"the task rewrite each other's rule every run: {wrong}")
