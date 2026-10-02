"""The keyset hand-off is ONE task, and it is the LAST task.

Everything in it is shown once and never again: the admin password, the console
break-glass password, the journal verification key whose server-side copy is
deleted, and the URLs to reach the panel. The installer prints the block it
hands over as one piece, so split across two tasks the user copies down half
of it.

Locked here because the failure is silent: a play that hands over the secrets
third from last still succeeds, still says nothing, and only costs the user
something later when they need the password nobody saved.

Run: uv run pytest tests/unit/test_dr_keyset_display_is_last.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
PLAYBOOK = ANSIBLE_DIR / "playbooks" / "show-keyset.yml"

FSS_PENDING = "/etc/catena/fss-verification-key.pending"


def _tasks() -> list[dict]:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert len(plays) == 1, "one play; the ordering claim below assumes it"
    return plays[0]["tasks"]


def _is_handoff(task: dict) -> bool:
    return "keyset_out" in str((task.get("ansible.builtin.copy") or {}).get("dest"))


def _handoff_tasks() -> list[dict]:
    return [t for t in _tasks() if _is_handoff(t)]


def _banner() -> str:
    return _handoff_tasks()[0]["vars"]["_banner"]


def test_the_handoff_is_the_last_task():
    assert _is_handoff(_tasks()[-1]), (
        "something runs after the block the user is copying down")


def test_there_is_exactly_one_handoff_task():
    """Two blocks means the earlier one scrolls away. One task, one block."""
    assert len(_handoff_tasks()) == 1


def test_the_handoff_reaches_the_controller_and_no_log():
    """It writes the installer's transient file on the controller, which the
    installer prints and deletes; the play's own output never carries it."""
    task = _handoff_tasks()[0]
    assert task["delegate_to"] == "localhost"
    assert task["become"] is False
    assert task["ansible.builtin.copy"]["mode"] == "0600"
    assert task["no_log"] is True


def test_the_shown_passwords_are_minted_before_the_handoff():
    """Right after bootstrap no converge has minted them yet, so this play
    mints them -- only them, adopting the install's pin first."""
    names = [t.get("name", "") for t in _tasks()]
    mint_at = next(i for i, t in enumerate(_tasks())
                   if "--mint-user-held" in str(t))
    assert mint_at < len(_tasks()) - 1, names
    mint = str(_tasks()[mint_at])
    assert "--no-mint" not in mint
    assert "--adopt-file" in mint and "admin_password is defined" in mint


def test_the_fss_key_is_deleted_before_it_is_handed_over():
    """Its content is already slurped into memory, so the block still carries
    it. Deleting first is what makes the removal unconditional -- a key left on
    the server silently voids the seal it exists to prove, and a delete that
    only runs after the hand-off is a delete that a failed render skips."""
    names = [t.get("name", "") for t in _tasks()]
    delete_at = next(
        i for i, t in enumerate(_tasks())
        if (t.get("ansible.builtin.file") or {}).get("path") == FSS_PENDING
    )
    handoff_at = next(i for i, t in enumerate(_tasks()) if _is_handoff(t))
    assert delete_at < handoff_at, names


def test_the_one_block_carries_both_the_secrets_and_the_urls():
    task_vars = _handoff_tasks()[0]["vars"]
    banner = task_vars["_banner"]
    for needed in ("admin_password", "console_recovery_password"):
        assert needed in banner, needed
    assert "catena_admin_hostname" in banner, "panel URL missing"
    assert "portainer_admin_hostname" in banner, "Portainer URL missing"
    # The SSH forward is the way in when Cloudflare or SSO is not working, so
    # it is the half that has to survive an edit. Its port comes through
    # _admin_port, which is where the role default is read.
    assert "_admin_port" in banner, "forwarded panel port missing"
    assert "catena_admin_ui_port" in task_vars["_admin_port"]
    assert "ssh -N -L" in banner and "catena_panel_user" in banner, (
        "the banner does not say how to open the forward, as which account")


def test_the_banner_names_portainers_username_and_it_is_not_the_email():
    """Portainer's admin username is the literal string `admin`, fixed at
    first boot by --admin-password-file (helpers/bootstrap_portainer_admin.py
    DEFAULT_ADMIN_USER). Keycloak takes admin_email. A banner that offers one
    "First login: <email>" line for both sends the user to Portainer with a
    username Portainer has never had, and the resulting failure reads as a
    wrong password -- for the one password they were told to save."""
    helper = (ANSIBLE_DIR / "helpers" / "bootstrap_portainer_admin.py").read_text()
    assert 'DEFAULT_ADMIN_USER = "admin"' in helper, (
        "Portainer's admin username moved; the banner below now lies")

    lines = _banner().splitlines()
    portainer_at = next(
        i for i, ln in enumerate(lines) if "portainer_admin_hostname" in ln)
    panel_at = next(
        i for i, ln in enumerate(lines) if "catena_admin_hostname" in ln)

    def username_after(start: int) -> str:
        for ln in lines[start:start + 4]:
            if "username:" in ln:
                return ln.split("username:", 1)[1].strip()
        raise AssertionError(f"no username line follows line {start}")

    assert username_after(portainer_at) == "admin", (
        "the Portainer entry does not name `admin` as its username")
    assert "admin_email" in username_after(panel_at), (
        "the panel entry no longer names admin_email")


def test_the_backup_password_is_generated_in_the_panel_not_shown_here():
    """No converge mints it, so the install has none to show; the banner says
    where it comes from instead."""
    banner = _banner()
    assert "backup_restic_password" not in banner
    assert "Settings > Backup" in banner


def test_the_public_urls_wait_for_a_domain():
    """With no domain the hostnames render as `https://dash.`, a name that
    resolves nowhere; the SSH forward is the way in until there is one. The
    names come from store facts, so the play publishes them first."""
    banner = _banner()
    for name in ("catena_admin_hostname", "portainer_admin_hostname"):
        line = next(i for i, ln in enumerate(banner.splitlines()) if name in ln)
        assert "catena_public_surface_deferred" in banner.splitlines()[line - 1], name
    pre = " ".join(str(t) for t in yaml.safe_load(PLAYBOOK.read_text())[0]["pre_tasks"])
    assert "seed_onbox_config" in pre
