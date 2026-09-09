"""Pin the admin state dir: the service argv and the role must agree.

The shell writes its audit hash chain and the per-host key that signs the
chain head into this directory. Three things have to line up or the chain
silently disables itself:

  1. the role creates the host dir,
  2. it is owned by the CONTAINER's uid (the image runs USER nonroot, 65532 --
     it was 1000 under the old Python shell, which left the distroless
     container unable to write its own state),
  3. the service mounts it READ-WRITE and the shell is pointed at it.

The argv builder is shared with the test bench, so a one-sided edit here is
a deploy that comes up without an audit chain and says nothing about it.

Run: uv run pytest tests/unit/test_catena_admin_state_bind.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[2]
_ROLE = _ANSIBLE / "reconcile" / "roles" / "catena-admin"
DEFAULTS = _ROLE / "defaults" / "main.yml"
# The bootstrap-side half of the same panel: the runner account, the
# sudoers drop-in, the forced command. Phase 1b made it a role of its
# own so the boundary it was declared on could actually be held.
_HOST_ROLE = (_ROLE.parents[2] / "bootstrap" / "roles"
              / "catena_admin_host")
_HOST_DEFAULTS = _HOST_ROLE / "defaults" / "main.yml"
# And the values both halves read, which belong to neither role.
_GROUP_VARS = (
    _ROLE.parents[2] / "playbooks" / "group_vars" / "all" / "main.yml")
HOST_TASKS = _HOST_ROLE / "tasks" / "main.yml"
PLUGIN = _ANSIBLE / "playbooks" / "filter_plugins" / "catena_admin_service.py"

STATE_DIR = "/var/lib/catena-admin"


def _plugin():
    spec = importlib.util.spec_from_file_location("catena_admin_service", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _defaults() -> dict:
    """Every variable the panel's converge reads, from all three places it now
    lives.

    Phase 1b split the role: the trust path is bootstrap/roles/catena_admin_host, the
    container is reconcile/roles/catena-admin, and the values BOTH halves need are in
    group_vars because a role default is only dependable once that role has run.
    Merged here so an assertion is about the panel's configuration rather than
    about which file happens to hold a line today.
    """
    merged: dict = {}
    for path in (_GROUP_VARS, DEFAULTS, _HOST_DEFAULTS):
        merged.update(yaml.safe_load(path.read_text()) or {})
    return merged


def _argv() -> list[str]:
    return _plugin().catena_admin_service_argv({
        "name": "catena-admin",
        "image": "ghcr.io/catenahq/catena-admin:latest",
        "network": "catena-network",
        "ui_port": "9010",
        "direct_port": "8001",
        "env": {"CATENA_ADMIN_STATE_DIR": STATE_DIR},
    })


def test_state_dir_default_matches_the_mount():
    assert _defaults()["catena_admin_state_dir"] == STATE_DIR
    argv = _argv()
    assert f"--mount=type=bind,source={STATE_DIR},destination={STATE_DIR}" \
        in argv, argv


def test_state_mount_is_writable():
    for arg in _argv():
        if arg.startswith(f"--mount=type=bind,source={STATE_DIR},"):
            assert not arg.endswith(",readonly"), (
                "the shell WRITES its audit chain here; a read-only mount "
                "disables tamper evidence without failing anything"
            )
            return
    raise AssertionError(f"{STATE_DIR} is not mounted")


def test_shell_is_pointed_at_the_state_dir():
    assert f"--env=CATENA_ADMIN_STATE_DIR={STATE_DIR}" in _argv(), (
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
