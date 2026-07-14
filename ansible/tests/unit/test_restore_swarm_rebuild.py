"""Lock the restore-path swarm/overlay rebuild.

The restic set excludes docker swarm raft + overlay IPAM (backup_paths
captures only .../docker/volumes); cluster runtime is rebuilt, not
restored. On a rollback restore the SAME live host keeps its
pre-restore .../docker/{swarm,network} on disk (restic never deletes
files absent from the snapshot), so dockerd comes back `active`, the
docker role skips `swarm init`, and swarm reconciles a stale raft +
endpoint table against just-restored volumes -- keycloak reattaches to
catena-network with a stale IPAM address and gets "No route to host".

These tests pin the fix: restore.yml removes both runtime dirs while
dockerd is stopped and only on a successful restore, so both DR and
rollback take the same clean `swarm init` + `network create` +
Portainer redeploy rebuild path.

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


def test_swarm_and_network_runtime_removed():
    task = _find("Discard surviving swarm + overlay runtime")
    spec = task["ansible.builtin.file"]
    assert spec["state"] == "absent"
    loop = task["loop"]
    assert any(item.endswith("/docker/swarm") for item in loop), (
        "swarm raft dir must be wiped so `swarm init` re-runs clean"
    )
    assert any(item.endswith("/docker/network") for item in loop), (
        "libnetwork overlay IPAM must be wiped so the stale endpoint "
        "table can't outlive the restore"
    )


def test_wipe_uses_storage_mount_point_not_docker_role_default():
    # docker_data_root is a docker-role default, out of scope in the
    # backup role's play; backup_paths already keys off storage_mount_point.
    task = _find("Discard surviving swarm + overlay runtime")
    loop = task["loop"]
    assert all("storage_mount_point" in item for item in loop)
    assert not any("docker_data_root" in item for item in loop)


def test_wipe_gated_on_successful_restore():
    # A failed restore must leave the runtime intact for diagnosis.
    task = _find("Discard surviving swarm + overlay runtime")
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
                if "Discard surviving swarm + overlay runtime" in n)
    unmask = next(i for i, n in enumerate(names)
                  if "Unmask docker.service" in n)
    assert wipe < unmask
