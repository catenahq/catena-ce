"""Pin the admin state dir: the compose and the role must agree.

The shell writes its audit hash chain and the per-host key that signs the
chain head into this directory. Three things have to line up or the chain
silently disables itself:

  1. the role creates the host dir,
  2. it is owned by the CONTAINER's uid (the image runs USER nonroot, 65532 --
     it was 1000 under the old Python shell, which left the distroless
     container unable to write its own state),
  3. the compose bind-mounts it READ-WRITE and points the shell at it.

The compose file is pushed verbatim by the bench, so a one-sided edit here is
a deploy that comes up without an audit chain and says nothing about it.

Run: uv run pytest tests/unit/test_catena_admin_state_bind.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "catena-admin"
)
DEFAULTS = _ROLE / "defaults" / "main.yml"
HOST_TASKS = _ROLE / "tasks" / "host.yml"
COMPOSE = _ROLE / "files" / "catena-admin.compose.yml"

STATE_DIR = "/var/lib/catena-admin"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["app"]


def test_state_dir_default_matches_the_compose_mount():
    assert _defaults()["catena_admin_state_dir"] == STATE_DIR
    mounts = _service()["volumes"]
    assert f"{STATE_DIR}:{STATE_DIR}" in mounts, mounts


def test_state_mount_is_writable():
    for m in _service()["volumes"]:
        if m.startswith(f"{STATE_DIR}:"):
            assert not m.endswith(":ro"), (
                "the shell WRITES its audit chain here; a read-only mount "
                "disables tamper evidence without failing anything"
            )
            return
    raise AssertionError(f"{STATE_DIR} is not mounted")


def test_shell_is_pointed_at_the_state_dir():
    env = _service()["environment"]
    assert env.get("CATENA_ADMIN_STATE_DIR") == STATE_DIR, (
        "without this the chain is disabled and the export silently degrades "
        "to unverifiable journald rows"
    )


def test_host_dir_is_owned_by_the_container_uid():
    tasks = yaml.safe_load(HOST_TASKS.read_text())
    for task in tasks:
        f = task.get("ansible.builtin.file") or {}
        if f.get("path") == "{{ catena_admin_state_dir }}":
            assert f["owner"] == "{{ catena_admin_container_uid }}", (
                "a literal uid here is how the dir ended up owned by 1000 "
                "while the distroless container runs as 65532"
            )
            assert f["group"] == "{{ catena_admin_container_gid }}"
            assert f["mode"] == "0700"
            return
    raise AssertionError("host.yml does not create the state dir")
