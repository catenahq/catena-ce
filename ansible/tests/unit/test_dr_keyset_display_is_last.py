"""The end-of-install display is ONE task, and it is the LAST task.

Everything in it is shown once and never again: the admin password, the restic
password that is the whole disaster-recovery story, the console break-glass
password, the journal verification key whose server-side copy is deleted, and
the URLs to reach the panel. Split across two tasks -- or with anything printed
after them -- the first block scrolls off and the user copies down half of it.

Locked here because the failure is silent: a play that prints the secrets third
from last still succeeds, still says nothing, and only costs the user something
later when they need the password nobody saved.

Run: uv run pytest tests/unit/test_dr_keyset_display_is_last.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
PLAYBOOK = ANSIBLE_DIR / "playbooks" / "show_dr_keyset.yml"

FSS_PENDING = "/etc/catena/fss-verification-key.pending"


def _tasks() -> list[dict]:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert len(plays) == 1, "one play; the ordering claim below assumes it"
    return plays[0]["tasks"]


def _debug_tasks() -> list[dict]:
    return [t for t in _tasks() if "ansible.builtin.debug" in t]


def test_the_display_is_the_last_task():
    assert "ansible.builtin.debug" in _tasks()[-1], (
        "something runs after the block the user is copying down"
    )


def test_there_is_exactly_one_display_task():
    """Two blocks means the earlier one scrolls away. One task, one block."""
    assert len(_debug_tasks()) == 1


def test_the_fss_key_is_deleted_before_it_is_shown():
    """Its content is already slurped into memory, so the banner still prints
    it. Deleting first is what makes the removal unconditional -- a key left on
    the server silently voids the seal it exists to prove, and a delete that
    only runs after a display task is a delete that a failed render skips."""
    names = [t.get("name", "") for t in _tasks()]
    delete_at = next(
        i for i, t in enumerate(_tasks())
        if (t.get("ansible.builtin.file") or {}).get("path") == FSS_PENDING
    )
    display_at = next(
        i for i, t in enumerate(_tasks()) if "ansible.builtin.debug" in t
    )
    assert delete_at < display_at, names


def test_the_one_block_carries_both_the_secrets_and_the_urls():
    task_vars = _debug_tasks()[0]["vars"]
    banner = task_vars["_banner"]
    for needed in ("admin_password", "backup_restic_password",
                   "console_recovery_password"):
        assert needed in banner, needed
    assert "infrastructure_dash_hostname" in banner, "panel URL missing"
    assert "portainer_admin_hostname" in banner, "Portainer URL missing"
    # The tailnet address is the way in when Cloudflare or SSO is not working,
    # so it is the half that has to survive an edit. Its port comes through
    # _admin_port, which is where the role default is read.
    assert "_admin_port" in banner, "tailnet panel port missing"
    assert "catena_admin_ui_port" in task_vars["_admin_port"]


def test_the_secrets_are_not_suppressed():
    """no_log on this task would print 'output has been hidden' and lose the
    only copy of three passwords. Showing them once is the entire point."""
    assert not _debug_tasks()[0].get("no_log", False)
