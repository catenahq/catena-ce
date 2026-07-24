"""Lock the converge's post-restore hook.

A restore drops /var/lib/catena/post-restore.needed and stops there: the
applications are down (image layers are not in the backup) and per-app
databases may not have survived their compose recreate. Everything that
closes that gap now lives in the catena-recovery host binary, invoked
once from site.yml's post_tasks.

Three properties have to hold for that to be safe, and none of them is
visible from reading the binary alone:

  1. It runs ONLY on the marker. Without the gate an ordinary converge
     would drop and reload every database from the last archives.
  2. It runs LAST, after every role. The applications must exist and
     Portainer must be up before anything tries to redeploy or replay.
  3. It carries no secret. The Portainer key is read from the on-box
     0600 file, so the task needs no no_log and its output stays
     readable when a recovery fails -- which is when someone needs it.

Run: uv run pytest tests/unit/test_post_restore_hook.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[3] / "ansible"
SITE = ANSIBLE / "playbooks" / "site.yml"
BACKUP_TASKS = ANSIBLE / "roles" / "backup" / "tasks" / "main.yml"
ADMIN_DEFAULTS = ANSIBLE / "roles" / "catena-admin" / "defaults" / "main.yml"

MARKER = "post-restore.needed"


def _play() -> dict:
    return yaml.safe_load(SITE.read_text())[0]


def _post_tasks() -> list[dict]:
    return _play()["post_tasks"]


def _hook() -> dict:
    for task in _post_tasks():
        argv = task.get("ansible.builtin.command", {}).get("argv") or []
        if "post-restore" in argv:
            return task
    raise AssertionError("site.yml has no post-restore hook")


def test_hook_is_gated_on_the_marker():
    task = _hook()
    when = task["when"]
    when = " ".join(when) if isinstance(when, list) else str(when)
    assert "stat.exists" in when, when

    stat_tasks = [
        t for t in _post_tasks()
        if MARKER in str(t.get("ansible.builtin.stat", {}).get("path", ""))
    ]
    assert stat_tasks, "nothing stats the marker"
    assert stat_tasks[0]["register"] in when


def test_hook_runs_after_every_role():
    # post_tasks is by definition after roles; the assertion that matters is
    # that the hook did not drift into pre_tasks or the role list, where
    # Portainer would not be up yet.
    play = _play()
    assert not any(
        "post-restore" in str(t) for t in play.get("pre_tasks", [])
    ), "the hook must not run before the roles"
    assert "post-restore" not in str(play["roles"])


def test_hook_replays_client_databases_not_the_infrastructure_one():
    # catena-postgres is restored byte for byte from its own volume. A
    # logical replay over it during recovery is pointless churn against the
    # database Keycloak is mid-start on.
    argv = _hook()["ansible.builtin.command"]["argv"]
    assert "-scope" in argv
    assert argv[argv.index("-scope") + 1] == "clients"


def test_hook_points_the_binary_at_the_marker_it_is_gated_on():
    # The binary clears the marker itself, and only on success. If the task
    # gated on one path and the binary cleared another, every converge would
    # redo the recovery forever.
    argv = _hook()["ansible.builtin.command"]["argv"]
    assert MARKER in argv[argv.index("-marker") + 1]


def test_hook_carries_no_secret():
    task = _hook()
    assert "environment" not in task, (
        "the Portainer key is read from the on-box file; putting it in the "
        "task environment would force no_log and hide the recovery log"
    )
    assert not task.get("no_log"), "the recovery log must stay readable"
    rendered = yaml.safe_dump(task)
    assert "vault_" not in rendered, rendered


def test_recovery_log_is_surfaced():
    debugs = [t for t in _post_tasks() if "ansible.builtin.debug" in t]
    assert any(
        "stderr_lines" in str(t["ansible.builtin.debug"]) for t in debugs
    ), "a half-finished recovery must not be swallowed behind a green task"


def test_binary_path_is_declared_by_the_role_that_installs_it():
    defaults = yaml.safe_load(ADMIN_DEFAULTS.read_text())
    assert defaults["catena_recovery_bin"].endswith("/catena-recovery")
    argv = _hook()["ansible.builtin.command"]["argv"]
    assert argv[0] == "{{ catena_recovery_bin }}"


def test_backup_role_no_longer_dispatches_the_replay_modes():
    # Two implementations of a replay is how they drift. The role keeps the
    # mechanisms a converge genuinely needs and nothing else.
    tasks = yaml.safe_load(BACKUP_TASKS.read_text())
    modes = set()
    for task in tasks:
        for key in ("ansible.builtin.include_tasks",):
            if key in task:
                modes.add(str(task[key]))
    assert not any("pg_replay" in m or "s3_reconcile" in m for m in modes), modes

    assert_task = tasks[0]["ansible.builtin.assert"]["that"]
    joined = " ".join(assert_task)
    for gone in ("pg_replay", "s3_reconcile"):
        assert gone not in joined, joined
    for kept in ("install", "verify", "restore"):
        assert kept in joined


def test_the_deleted_task_files_are_actually_gone():
    for name in ("pg_replay.yml", "s3_reconcile.yml"):
        assert not (ANSIBLE / "roles" / "backup" / "tasks" / name).exists()
    assert not (ANSIBLE / "playbooks" / "filter_plugins" / "topo_sort.py").exists()
