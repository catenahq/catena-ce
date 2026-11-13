"""bootstrap/roles/catena_admin_host/tasks/validate.yml checks the accounts
(shadow password `*`, not locked), the paths' owners by number for the
container's uid, known_hosts keyed on the alias the panel dials, and the
migration account's rules as sshd resolves them (`sshd -T -C`).

Run: uv run pytest tests/unit/test_catena_admin_host_validate.py
"""
from __future__ import annotations

import yaml

from ansible_tree import role_dir

ROLE = role_dir("catena_admin_host")
TASKS = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())


def _task(fragment: str) -> dict:
    for t in TASKS:
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _that(fragment: str) -> str:
    return " ".join(_task(fragment)["ansible.builtin.assert"]["that"])


def test_both_accounts_are_checked_for_shell_and_home():
    accounts = _task("the accounts to check")["ansible.builtin.set_fact"]["_cah_accounts"]
    assert {(a["user"], a["shell"], a["home"]) for a in accounts} == {
        ("{{ catena_admin_runner_user }}", "/bin/bash", "{{ catena_admin_runner_home }}"),
        ("{{ catena_migrate_user }}", "/bin/sh", "{{ catena_migrate_home }}"),
    }
    that = _that("exist with their shell and home")
    assert "[4] == item.item.home" in that and "[5] == item.item.shell" in that


def test_neither_account_may_be_locked():
    assert _task("read both accounts' shadow")["ansible.builtin.getent"]["database"] == "shadow"
    assert "getent_shadow[item.item.user][0] == '*'" in _that("neither account is locked")


def test_container_owned_paths_are_checked_by_number():
    paths = _task("the dirs, keys and config")["vars"]["_assert_paths"]
    by_path = {p["path"]: p for p in paths}
    for name in ("", "/id_ed25519", "/id_ed25519.pub", "/known_hosts", "/config"):
        entry = by_path["{{ catena_admin_host_ssh_dir }}" + name]
        assert entry["uid"] == "{{ catena_container_uid }}" and "owner" not in entry
        assert entry["gid"] == "{{ catena_container_gid }}"
    state = by_path["{{ catena_admin_state_dir }}"]
    assert (state["uid"], state["mode"]) == ("{{ catena_container_uid }}", "0700")
    assert by_path["{{ catena_export_dir }}"]["gid"] == "{{ catena_export_dir_group }}"
    assert by_path["/etc/ssh/sshd_config.d/31-catena-migrate.conf"]["mode"] == "0644"


def test_known_hosts_is_keyed_on_the_alias_and_never_empty():
    that = _that("known_hosts holds the host key")
    assert "_known_hosts | length > 0" in that
    assert "^host.docker.internal ssh-ed25519 " in that.replace("\\.", ".")


def test_the_client_config_must_verify_host_keys():
    assert "StrictHostKeyChecking yes" in _that("the ssh client config verifies")


def test_the_migration_rules_are_read_from_sshd_not_from_the_file():
    argv = _task("sshd's rules for the migration account")["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["/usr/sbin/sshd", "-T", "-C"]
    assert argv[3] == "user={{ catena_migrate_user }},host=localhost,addr=127.0.0.1"
    assert _task("sshd's rules for the migration account")["changed_when"] is False


def test_every_restriction_is_asserted_in_sshd_t_form():
    wanted = _task("can only run the lane's pipe")["vars"]["_wanted"]
    assert sorted(wanted) == sorted([
        "authorizedkeysfile {{ catena_migrate_grant_file }}",
        "forcecommand {{ catena_migrate_connect }}",
        "authorizedkeyscommand none",
        "permittty no",
        "allowtcpforwarding no",
        "allowstreamlocalforwarding no",
        "allowagentforwarding no",
        "x11forwarding no",
        "permittunnel no",
        "permituserrc no",
    ])
