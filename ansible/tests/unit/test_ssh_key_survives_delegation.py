"""The controller's SSH key reaches tasks delegated to localhost.

bootstrap.yml's key install and key checks run `delegate_to: localhost` and
hand {{ ansible_ssh_private_key_file }} to ssh. Under delegation Ansible
templates `ansible_connection` as localhost's `local`, so a definition that
reads it bare yields an empty key there: install_key.py then verified with no
identity and failed a fresh install ("key auth verification failed after 5
attempts", bench run 2026-09-26T23-13-35-33b6). The host's own connection is
`hostvars[inventory_hostname].ansible_connection`, which reads `local` only on
the inventory catena-converge writes.

Checked against ansible-core 2.20: delegated, the hostvars form gives the key
and the bare form gives ''; on a local-connection host both give ''.

Run: uv run pytest tests/unit/test_ssh_key_survives_delegation.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
GROUP_VARS = ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml"


def test_the_key_reads_the_hosts_own_connection():
    expr = yaml.safe_load(GROUP_VARS.read_text())["ansible_ssh_private_key_file"]
    assert "lookup('dotenv', 'SSH_PRIVATE_KEY')" in expr, expr
    bare = re.findall(r"(?<![.\w])ansible_connection\b", expr)
    assert not bare, (
        "ansible_ssh_private_key_file reads ansible_connection bare; a task "
        "delegated to localhost then gets an empty key:\n" + expr)
    assert "hostvars[inventory_hostname].ansible_connection" in expr, expr
