"""Lock the quiesce + state-register dispatch wiring.

The host action table maps a NAME to a FIXED command string. That is the
whole security property of the dispatcher: the container names an action,
it never composes one. Two consequences this file pins down.

  1. A request that varies per application (which app did the client
     stop?) cannot travel in argv. It travels as an opaque PAYLOAD on
     stdin, exactly like the settings and restic actions.
  2. These are COMMUNITY actions. The register rides every backup and a
     restore reads it on any edition, so a host with no license still
     has to be able to record and honour "leave this one off". The
     licence gates the migration panel that drives quiesce, not the
     mechanism.

Run: uv run pytest tests/unit/test_catena_admin_quiesce_actions.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"

QUIESCE_ACTIONS = {
    "catena-quiesce-pause",
    "catena-quiesce-stop",
    "catena-quiesce-resume",
    "catena-quiesce-status",
}
REGISTER_ACTIONS = {"catena-register-set", "catena-register-get"}
RESTORE_ACTIONS = {
    "catena-restore-run",
    "catena-restore-status",
    "catena-restore-reset",
}


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _ce_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ce_reserved_actions"]}


def test_actions_are_community_not_business():
    ce = set(_ce_actions())
    assert QUIESCE_ACTIONS <= ce
    assert REGISTER_ACTIONS <= ce
    assert RESTORE_ACTIONS <= ce

    ee = {a["name"] for a in _defaults()["catena_admin_ee_reserved_actions"]}
    assert not (QUIESCE_ACTIONS | REGISTER_ACTIONS | RESTORE_ACTIONS) & ee, (
        "the mechanism ships on every edition; only the migration panel "
        "that drives it is licensed"
    )


def test_every_action_targets_the_recovery_binary():
    ce = _ce_actions()
    for name in QUIESCE_ACTIONS | REGISTER_ACTIONS | {
        "catena-restore-run", "catena-restore-reset",
    }:
        assert "{{ catena_recovery_bin }}" in ce[name], name


def test_restore_runs_detached_from_the_dispatching_session():
    # The dispatcher's SSH session is a child of the admin container's
    # connection. The restore deliberately does not stop the panel, but a
    # panel restart for any other reason would otherwise kill the restore.
    shell = _ce_actions()["catena-restore-run"]
    assert "systemd-run" in shell
    assert "--unit catena-recovery-restore" in shell
    assert "--collect" in shell, (
        "without --collect a finished unit blocks the next attempt"
    )


def test_restore_request_travels_on_stdin_not_in_argv():
    shell = _ce_actions()["catena-restore-run"]
    assert "$PAYLOAD" in shell and "base64 -d" in shell
    assert "--pipe" in shell, "systemd-run needs --pipe to forward stdin"
    assert "restore -stdin" in shell


def test_status_does_not_clear_what_it_reports():
    # A page refresh must not destroy the state it is refreshing.
    shell = _ce_actions()["catena-restore-status"]
    assert "-reset" not in shell
    assert "{{ catena_recovery_state_file }}" in shell
    assert "{{ catena_recovery_history_file }}" in shell


def test_reset_is_its_own_deliberate_action():
    assert _ce_actions()["catena-restore-reset"].strip().endswith("restore -reset")


def test_run_state_lives_outside_the_backup_set():
    # /etc rides every snapshot. A restore's progress file there would travel
    # into the next host that restored that snapshot, which would then think a
    # restore was already in flight.
    d = _defaults()
    for key in ("catena_recovery_state_file", "catena_recovery_history_file"):
        assert d[key].startswith("/var/lib/catena/"), d[key]


def test_quiesce_actions_take_no_argument():
    # A fixed command per action is what makes the allow-list meaningful.
    ce = _ce_actions()
    for name, verb in (
        ("catena-quiesce-pause", "pause"),
        ("catena-quiesce-stop", "stop"),
        ("catena-quiesce-resume", "resume"),
        ("catena-quiesce-status", "status"),
    ):
        assert ce[name].strip().endswith(f"quiesce {verb}"), ce[name]
        assert "$" not in ce[name], (
            f"{name} interpolates a variable; the dispatcher's contract is a "
            "fixed command per name"
        )


def test_register_set_reads_its_request_from_stdin():
    # The app name varies per call, so it cannot be in the fixed command.
    shell = _ce_actions()["catena-register-set"]
    assert "$PAYLOAD" in shell
    assert "base64 -d" in shell
    assert "register set-stdin" in shell


def test_register_get_is_read_only():
    shell = _ce_actions()["catena-register-get"]
    assert shell.strip().endswith("register get")
    assert "$" not in shell


def test_recovery_binary_path_is_declared_once():
    d = _defaults()
    assert d["catena_recovery_bin"].endswith("/catena-recovery")
