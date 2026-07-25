"""Lock the migration lane's CE-side wiring.

The lane itself is a host process in the private repo. What lives here is
everything that decides how it is reachable and how it is armed, and each
of those has a way of going quietly wrong:

  1. The port has to be declared tailnet-only in the public-port
     registry. The lane binds its tailnet address and refuses to start
     without one, so the declaration is defence in depth -- but it is
     ALSO what puts the port in the effective set validation reads. An
     undeclared listener reads as an unexpected open port.
  2. Arming is a Business action name, authorized in the host dispatch
     table like every other Business name. The mechanism ships on every
     host; the panel that opens a window is what is licensed.
  3. Resume must NOT go through the lane. Putting a source back is the
     inverse of a quiesce, and it has to work whether or not a migration
     window is still open -- including when the lane itself is what
     failed.

Run: uv run pytest tests/unit/test_catena_admin_migrate_lane.py
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"
HOST_TASKS = _ROLE / "tasks" / "host.yml"

MIGRATE_ACTIONS = {
    "catena-migrate-arm",
    "catena-migrate-disarm",
    "catena-migrate-status",
    "catena-migrate-resume",
    # Target side: this server pulling another one onto itself.
    "catena-migrate-run",
    "catena-migrate-run-status",
    "catena-migrate-run-reset",
    "catena-migrate-resume-source",
}


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _ee_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ee_reserved_actions"]}


def _ce_actions() -> dict[str, str]:
    return {a["name"]: a["shell"] for a in _defaults()["catena_admin_ce_reserved_actions"]}


def test_lane_paths_are_declared_once():
    d = _defaults()
    assert d["catena_migrate_lane_bin"].endswith("/catena-migrate-lane")
    assert int(d["catena_migrate_lane_port"]) == 9040


def test_arming_is_a_business_action_name():
    ee = _ee_actions()
    assert MIGRATE_ACTIONS <= set(ee), sorted(MIGRATE_ACTIONS - set(ee))
    # Not duplicated into the Community list: a Community host has the lane
    # binary (the payload is ungated) but no panel that arms it.
    assert not MIGRATE_ACTIONS & set(_ce_actions())


def test_arm_prints_json_for_the_panel():
    shell = _ee_actions()["catena-migrate-arm"]
    assert "{{ catena_migrate_lane_bin }}" in shell
    assert shell.strip().endswith("arm -json"), shell
    # The pairing code is printed once and never stored, so nothing here may
    # redirect it into a file.
    assert ">" not in shell and "tee" not in shell


def test_disarm_and_status_take_no_argument():
    ee = _ee_actions()
    for name, verb in (
        ("catena-migrate-disarm", "disarm"),
        ("catena-migrate-status", "status"),
    ):
        assert ee[name].strip().endswith(f"{verb}"), ee[name]
        assert "$" not in ee[name], (
            f"{name} interpolates a value; the dispatcher's contract is a "
            "fixed command per name"
        )


def test_resume_does_not_go_through_the_lane():
    # Putting a source back has to work when the lane is what failed.
    shell = _ee_actions()["catena-migrate-resume"]
    assert "{{ catena_recovery_bin }}" in shell
    assert "catena_migrate_lane_bin" not in shell
    assert shell.strip().endswith("quiesce resume")


def test_target_side_state_files_are_separate_from_the_restores():
    # A migration DRIVES a restore, so sharing one state file would have the
    # page watching the move read the restore's progress as its own.
    d = _defaults()
    assert d["catena_migration_state_file"] != d["catena_recovery_state_file"]
    assert d["catena_migration_history_file"] != d["catena_recovery_history_file"]
    # Under /var/lib, not /etc: /etc is in the backup set, and a snapshot taken
    # mid-move would otherwise carry a half-finished migration's state into the
    # next host that restored it.
    for key in ("catena_migration_state_file", "catena_migration_history_file"):
        assert d[key].startswith("/var/lib/catena/"), d[key]


def test_the_move_runs_detached_and_takes_its_request_on_stdin():
    shell = _ee_actions()["catena-migrate-run"]
    # Detached, because a move runs for hours and must not die with the panel
    # container that started it.
    assert "systemd-run" in shell and "--collect" in shell
    assert "--pipe" in shell, (
        "without --pipe the request cannot reach the binary on stdin, which is "
        "what keeps the pairing code out of argv"
    )
    assert shell.strip().endswith("run --stdin"), shell
    # The pairing code travels inside $PAYLOAD and must never appear as an
    # argument: argv is readable by every process on the host, for hours.
    assert "--code" not in shell and "--source" not in shell


def test_resume_source_is_a_separate_action_from_the_move():
    # Once the source is stopped, putting it back is a decision. Folding it into
    # the move would make a retry and an abort the same button.
    ee = _ee_actions()
    assert "catena-migrate-resume-source" in ee
    assert ee["catena-migrate-resume-source"] != ee["catena-migrate-run"]
    shell = ee["catena-migrate-resume-source"]
    assert shell.strip().endswith("resume-source --stdin"), shell
    # Not detached: it is a single call to the source and its answer is what the
    # person who pressed it is waiting for.
    assert "systemd-run" not in shell


def test_the_move_status_action_is_read_only():
    shell = _ee_actions()["catena-migrate-run-status"]
    # A status action that cleared state would let a page refresh destroy what
    # it reported.
    assert "reset" not in shell
    assert "{{ catena_migration_state_file }}" in shell
    assert "{{ catena_migration_history_file }}" in shell
    assert "head -n 1" in shell, (
        "the state file carries the source and snapshot on later lines; reading "
        "all of it would report them as the state"
    )


def test_reset_forgets_the_record_and_nothing_else():
    shell = _ee_actions()["catena-migrate-run-reset"]
    assert shell.strip().endswith("reset"), shell
    # No resume, no restore, no DNS: forgetting a move and undoing one are
    # different acts.
    for forbidden in ("resume", "restore", "cutover", "rm "):
        assert forbidden not in shell, shell


def test_lane_port_is_declared_tailnet_only():
    body = HOST_TASKS.read_text()
    assert "catena_migrate_lane_port" in body, (
        "the lane's port is not declared in the public-port registry, so "
        "validation would read it as an unexpected open port"
    )
    # The declaration block: the lane is a host process, so bind=host (not the
    # docker DNAT path the panel's published port uses), and tailnet scope.
    start = body.index("declare UI + migration-lane ports")
    block = body[start:start + 1400]
    assert '"port": catena_migrate_lane_port | int' in block
    assert '"scope": "tailnet"' in block
    assert '"bind": "host"' in block
    assert '"scope": "any"' not in block


def test_lane_port_does_not_collide_with_the_panel_port():
    # The panel's port is a dotenv lookup with a default; the lane's is a
    # literal. Compare against that default, parsed out rather than retyped, so
    # a bump of either side is caught here instead of on a host where two
    # things then fight over one port.
    d = _defaults()
    m = re.search(r"default='(\d+)'", str(d["catena_admin_ui_port"]))
    assert m, f"could not read the panel port default: {d['catena_admin_ui_port']!r}"
    assert int(d["catena_migrate_lane_port"]) != int(m.group(1))
