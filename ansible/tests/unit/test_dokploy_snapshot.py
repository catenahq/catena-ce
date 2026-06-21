"""Guards the snapshot-before-converge feature (Recommendation E from
the pre-sales hardening review).

Before each converge that touches Dokploy state, every project's every
compose (with current composeFile + env + domains) gets dumped to a
JSON aggregate under {{ dokploy_snapshots_dir }}. Cheap insurance
against operator misclick or seeder bug overwriting client edits.

Three layers of contract pinned here:
  1. The snapshot include is wired into roles/infrastructure/tasks/
     main.yml BEFORE any compose-touching include.
  2. The snapshot task itself is best-effort (block/rescue) so a
     Dokploy API hiccup doesn't block the converge.
  3. The defaults expose dokploy_snapshots_dir +
     dokploy_snapshots_keep_last so operators can override per-host.

If any of these fail, fix the code, not the test.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
INFRA_TASKS_DIR = REPO / "ansible" / "roles" / "infrastructure" / "tasks"
INFRA_DEFAULTS = REPO / "ansible" / "roles" / "infrastructure" / "defaults" / "main.yml"
INFRA_MAIN = INFRA_TASKS_DIR / "main.yml"
SNAPSHOT_TASK = INFRA_TASKS_DIR / "snapshot_dokploy_state.yml"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_snapshot_task_file_exists_and_parses():
    assert SNAPSHOT_TASK.exists(), (
        "snapshot_dokploy_state.yml must exist under "
        "roles/infrastructure/tasks/ -- it's the entry point for "
        "Recommendation E from the pre-sales hardening plan."
    )
    tasks = _load(SNAPSHOT_TASK)
    assert isinstance(tasks, list) and tasks, (
        "snapshot_dokploy_state.yml must parse as a non-empty list of "
        "Ansible tasks."
    )


def test_snapshot_task_is_best_effort_via_block_rescue():
    """A Dokploy API hiccup must not block the converge. The capture
    runs under block/rescue; the rescue logs a warning and the play
    continues. Removing rescue would make a 503 from Dokploy
    catastrophic for every nightly converge."""
    tasks = _load(SNAPSHOT_TASK)
    # Find the capture block -- there's exactly one block:/rescue: pair.
    block_tasks = [t for t in tasks if isinstance(t, dict) and "block" in t and "rescue" in t]
    assert block_tasks, (
        "snapshot_dokploy_state.yml must wrap the capture logic in a "
        "block/rescue so a Dokploy API hiccup logs + continues rather "
        "than blocking the whole converge."
    )


def test_snapshot_task_writes_iso_timestamped_aggregate():
    """The aggregate filename must be lex-sortable = chronological.
    The rotation step relies on `sort(attribute='path')` to identify
    oldest-first. ISO basic short (no colons, sortable as a string)
    is the only safe choice."""
    text = SNAPSHOT_TASK.read_text(encoding="utf-8")
    assert "iso8601_basic_short" in text, (
        "snapshot_dokploy_state.yml must use ansible_date_time."
        "iso8601_basic_short for the aggregate filename -- the rotation "
        "logic sorts by path and breaks if the timestamp isn't lex-"
        "sortable as chronological."
    )
    assert "dokploy-state-" in text, (
        "aggregate filename must use the canonical 'dokploy-state-' "
        "prefix so the rotation `find` pattern catches it."
    )


def test_snapshot_task_rotates_oldest_aggregates():
    """Without rotation, snapshots accumulate forever. The defaults
    set keep_last=30; the task must compute + delete the excess."""
    text = SNAPSHOT_TASK.read_text(encoding="utf-8")
    assert "dokploy_snapshots_keep_last" in text, (
        "snapshot_dokploy_state.yml must reference "
        "dokploy_snapshots_keep_last for rotation."
    )
    assert "_ds_to_delete" in text, (
        "rotation must use a _ds_to_delete fact computed from the "
        "list of existing aggregates minus the last keep_last."
    )


def test_snapshot_defaults_published():
    """Operators must be able to override the snapshot dir + retention
    per inventory -- defaults live in roles/infrastructure/defaults/
    main.yml."""
    defaults = _load(INFRA_DEFAULTS)
    assert "dokploy_snapshots_dir" in defaults, (
        "dokploy_snapshots_dir must be exposed as a role default for "
        "per-host override."
    )
    assert "dokploy_snapshots_keep_last" in defaults, (
        "dokploy_snapshots_keep_last must be exposed as a role default "
        "for per-host retention tuning."
    )
    keep = defaults["dokploy_snapshots_keep_last"]
    assert isinstance(keep, int) and keep > 0, (
        f"dokploy_snapshots_keep_last must be a positive integer "
        f"(got {keep!r})."
    )


def test_snapshot_wired_into_infrastructure_main_before_composes():
    """The snapshot include must run BEFORE any compose-touching
    include (gatus, healthchecks, homepage, recovery,
    dokploy_templates) so the aggregate captures pre-mutation state. Otherwise a converge that overwrites a client edit would
    snapshot the post-overwrite state and the operator couldn't roll
    back the change.

    Match the actual include statement (`file: <name>.yml`), not bare
    occurrences of the filename -- header comments in main.yml mention
    several task filenames before the actual includes appear."""
    import re
    main_text = INFRA_MAIN.read_text(encoding="utf-8")
    snap_match = re.search(r'^\s*file:\s*snapshot_dokploy_state\.yml',
                           main_text, re.MULTILINE)
    assert snap_match, (
        "snapshot_dokploy_state.yml is not included from "
        "roles/infrastructure/tasks/main.yml. Recommendation E is not "
        "wired into the converge."
    )
    snap_pos = snap_match.start()

    # Every compose-touching include must have its `file: ...` line
    # AFTER the snapshot's. Match the include statement, not header
    # comment mentions.
    for compose_task in (
        "gatus.yml",
        "healthchecks.yml",
        "homepage.yml",
        "recovery.yml",
        "dokploy_templates.yml",
    ):
        m = re.search(rf'^\s*file:\s*{re.escape(compose_task)}\b',
                      main_text, re.MULTILINE)
        if m is None:
            continue  # task may not exist in all configs
        assert m.start() > snap_pos, (
            f"{compose_task} include is positioned BEFORE the snapshot "
            f"include in roles/infrastructure/tasks/main.yml. The "
            f"snapshot must capture pre-mutation state."
        )


def test_snapshot_task_yaml_loads_cleanly():
    """Regression: a Jinja templating error in the aggregate-build
    set_fact would surface as a YAML/Ansible parse error at converge
    time, not at lint time. Make sure the file at least loads."""
    tasks = _load(SNAPSHOT_TASK)
    assert isinstance(tasks, list) and len(tasks) >= 2, (
        "snapshot_dokploy_state.yml must parse as a list with at "
        "least the dir-create + capture-block tasks."
    )
