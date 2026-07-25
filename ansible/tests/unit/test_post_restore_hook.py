"""Lock the converge's post-restore seam.

A restore drops /var/lib/catena/post-restore.needed and stops there: the
applications are down (image layers are not in the backup) and per-app
databases may not have survived their compose recreate. Finishing that is
day-2 work, which this repo does not own.

So the converge provides a MOMENT, not an implementation. It runs
whatever is installed in /etc/catena/post-restore.d and knows nothing
about the contents -- the same shape as /etc/catena/quiesce.d, whose
hooks the on-host daily chain runs without this repo knowing them.

Four properties have to hold, and none is visible from reading a hook:

  1. It runs ONLY on the marker. Without the gate an ordinary converge
     would re-run a recovery, which drops and reloads every database.
  2. It runs LAST, after every role, because the hooks need the control
     plane the roles brought up. A trigger that fired when the marker
     APPEARED would run before docker exists on the DR path.
  3. This repo names no binary from another repo, and an empty hook
     directory converges exactly as it did before the seam existed.
  4. A failing hook fails the converge. A converge that left a recovery
     unfinished is not a successful converge.

Run: uv run pytest tests/unit/test_post_restore_hook.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[3] / "ansible"
SITE = ANSIBLE / "playbooks" / "site.yml"
BACKUP_TASKS = ANSIBLE / "roles" / "backup" / "tasks" / "main.yml"
BACKUP_INSTALL = ANSIBLE / "roles" / "backup" / "tasks" / "install.yml"
BACKUP_DEFAULTS = ANSIBLE / "roles" / "backup" / "defaults" / "main.yml"

MARKER = "post-restore.needed"
HOOK_DIR_VAR = "catena_post_restore_hook_dir"


def _play() -> dict:
    return yaml.safe_load(SITE.read_text())[0]


def _post_tasks() -> list[dict]:
    return _play()["post_tasks"]


def _hook_task() -> dict:
    for task in _post_tasks():
        body = task.get("ansible.builtin.shell")
        if body and HOOK_DIR_VAR in str(body):
            return task
    raise AssertionError("site.yml has no post-restore hook runner")


def test_hook_runner_is_gated_on_the_marker():
    when = _hook_task()["when"]
    when = " ".join(when) if isinstance(when, list) else str(when)
    assert "stat.exists" in when, when

    stat_tasks = [
        t for t in _post_tasks()
        if MARKER in str(t.get("ansible.builtin.stat", {}).get("path", ""))
    ]
    assert stat_tasks, "nothing stats the marker"
    assert stat_tasks[0]["register"] in when


def test_hook_runner_is_in_post_tasks_not_earlier():
    # The hooks need the control plane the roles above brought up.
    play = _play()
    assert not any(HOOK_DIR_VAR in str(t) for t in play.get("pre_tasks", []))
    assert HOOK_DIR_VAR not in str(play["roles"])


def test_this_repo_names_no_binary_from_another_repo():
    # The whole point of the seam. catena-ce sets the scene; whatever manages
    # the host afterwards subscribes by installing a hook. A named binary here
    # would invert that and also fail the converge on a host that has not
    # installed it.
    body = str(_hook_task()["ansible.builtin.shell"])
    for leaked in ("catena-recovery", "catena_recovery_bin", "post-restore -scope"):
        assert leaked not in body, (
            f"{leaked!r} appears in the converge; the seam must not know what "
            "the hooks do"
        )


def test_an_empty_hook_directory_is_a_no_op():
    # A host with no hooks must converge exactly as it did before the seam.
    body = str(_hook_task()["ansible.builtin.shell"])
    assert '[ -f "$hook" ] || continue' in body, (
        "the loop must tolerate an empty directory (the glob stays literal)"
    )
    changed = str(_hook_task()["changed_when"])
    assert "HOOKS_RAN=0" in changed, (
        "running zero hooks must report unchanged, or every converge on a "
        "marker-less host reports changed"
    )


def test_a_failing_hook_fails_the_converge():
    body = str(_hook_task()["ansible.builtin.shell"])
    assert 'failed=$((failed + 1))' in body
    assert '[ "$failed" -eq 0 ]' in body, (
        "the script must exit non-zero when any hook failed; a converge that "
        "left a recovery unfinished is not a successful converge"
    )


def test_hook_output_is_surfaced():
    debugs = [t for t in _post_tasks() if "ansible.builtin.debug" in t]
    assert any(
        "stdout_lines" in str(t["ansible.builtin.debug"]) for t in debugs
    ), "a half-finished recovery must not be swallowed behind a green task"


def test_this_repo_creates_the_directory_and_never_writes_to_it():
    tasks = yaml.safe_load(BACKUP_INSTALL.read_text())
    creators = [
        t for t in tasks
        if HOOK_DIR_VAR in str(t.get("ansible.builtin.file", {}).get("path", ""))
    ]
    assert len(creators) == 1, "exactly one task should create the hook directory"
    assert creators[0]["ansible.builtin.file"]["state"] == "directory"

    # Nothing in this repo may install a hook: that is the other side's job.
    for path in (BACKUP_INSTALL, SITE):
        body = path.read_text()
        assert "post-restore.d/" not in body.replace(
            "/etc/catena/post-restore.d\n", ""
        ), f"{path.name} writes into the hook directory"


def test_hook_directory_rides_the_backup():
    # Under /etc/catena, which is in backup_paths, so a restored host keeps
    # the hooks it had.
    defaults = yaml.safe_load(BACKUP_DEFAULTS.read_text())
    assert defaults[HOOK_DIR_VAR].startswith("/etc/catena/")


def test_backup_role_no_longer_dispatches_the_replay_modes():
    # Two implementations of a replay is how they drift. The role keeps the
    # mechanisms a converge genuinely needs and nothing else.
    tasks = yaml.safe_load(BACKUP_TASKS.read_text())
    modes = {
        str(t["ansible.builtin.include_tasks"])
        for t in tasks if "ansible.builtin.include_tasks" in t
    }
    assert not any("pg_replay" in m or "s3_reconcile" in m for m in modes), modes

    joined = " ".join(tasks[0]["ansible.builtin.assert"]["that"])
    for gone in ("pg_replay", "s3_reconcile"):
        assert gone not in joined, joined
    for kept in ("install", "verify", "restore"):
        assert kept in joined


def test_the_deleted_task_files_are_actually_gone():
    for name in ("pg_replay.yml", "s3_reconcile.yml"):
        assert not (ANSIBLE / "roles" / "backup" / "tasks" / name).exists()
    assert not (ANSIBLE / "playbooks" / "filter_plugins" / "topo_sort.py").exists()
