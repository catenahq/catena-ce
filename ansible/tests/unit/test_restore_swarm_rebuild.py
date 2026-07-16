"""Lock the restore-path swarm raft rebuild.

The restic set excludes docker swarm raft + overlay IPAM (backup_paths
captures only .../docker/volumes); cluster runtime is rebuilt, not
restored. On a rollback restore the SAME live host keeps its
pre-restore .../docker/swarm on disk (restic never deletes files absent
from the snapshot), so dockerd comes back `active`, the docker role
skips `swarm init`, and swarm reconciles a stale raft + endpoint table
against just-restored volumes -- keycloak reattaches to catena-network
with a stale IPAM address and gets "No route to host".

The wipe targets ONLY docker/swarm, NOT docker/network. catena-network
is an OVERLAY (definition + IPAM in the raft), so wiping the raft is
what rebuilds it clean. The per-stack compose BRIDGES are LOCAL-scoped
(docker/network's local-kv.db); wiping that dir destroyed every app's
bridge and CE does not redeploy template-app stacks on a converge
(EE post_restore_redeploy owns that), so Nextcloud/mailserver/... stayed
Exited(128) "network not found" after every CE rollback. Keeping
docker/network lets those containers restart on their surviving bridge.

These tests pin the fix: restore.yml removes docker/swarm (and NOT
docker/network) while dockerd is stopped and only on a successful
restore, so the overlay rebuilds clean while compose bridges survive.

Run: uv run pytest tests/unit/test_restore_swarm_rebuild.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

TASKS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "backup" / "tasks" / "restore.yml"
)


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _find(name_fragment: str) -> dict:
    for task in _tasks():
        if name_fragment in (task.get("name") or ""):
            return task
    raise AssertionError(f"task not found: {name_fragment!r}")


def test_swarm_raft_removed_and_network_preserved():
    task = _find("Discard surviving swarm raft")
    spec = task["ansible.builtin.file"]
    assert spec["state"] == "absent"
    path = spec["path"]
    assert path.endswith("/docker/swarm"), (
        "swarm raft dir must be wiped so `swarm init` re-runs clean and the "
        "overlay (defined in raft) rebuilds without stale IPAM"
    )
    # The regression guard: docker/network holds the LOCAL-scoped compose
    # bridges. Wiping it stranded every template app after a CE rollback.
    assert not path.endswith("/docker/network"), (
        "docker/network must be PRESERVED -- it holds the per-stack compose "
        "bridges; CE does not redeploy template-app stacks, so wiping it "
        "leaves them Exited(128) 'network not found'"
    )
    assert "loop" not in task, (
        "the wipe targets a single path (docker/swarm); a loop reintroduces "
        "the docker/network deletion this fix removed"
    )


def test_wipe_uses_storage_mount_point_not_docker_role_default():
    # docker_data_root is a docker-role default, out of scope in the
    # backup role's play; backup_paths already keys off storage_mount_point.
    task = _find("Discard surviving swarm raft")
    path = task["ansible.builtin.file"]["path"]
    assert "storage_mount_point" in path
    assert "docker_data_root" not in path


def test_wipe_gated_on_successful_restore():
    # A failed restore must leave the runtime intact for diagnosis.
    task = _find("Discard surviving swarm raft")
    when = task["when"]
    cond = " ".join(when) if isinstance(when, list) else when
    assert "_restic_restore.rc" in cond
    assert "== 0" in cond


def test_wipe_runs_before_docker_unmask():
    # dockerd stays stopped through the play, but the wipe must land
    # before the unmask so intent is unambiguous: rebuild happens while
    # the daemon is down.
    names = [(t.get("name") or "") for t in _tasks()]
    wipe = next(i for i, n in enumerate(names)
                if "Discard surviving swarm raft" in n)
    unmask = next(i for i, n in enumerate(names)
                  if "Unmask docker.service" in n)
    assert wipe < unmask
