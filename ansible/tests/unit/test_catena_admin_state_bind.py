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
_ROLE = _ANSIBLE / "roles" / "catena-admin"
DEFAULTS = _ROLE / "defaults" / "main.yml"
HOST_TASKS = _ROLE / "tasks" / "host.yml"
PLUGIN = _ANSIBLE / "playbooks" / "filter_plugins" / "catena_admin_service.py"

STATE_DIR = "/var/lib/catena-admin"


def _plugin():
    spec = importlib.util.spec_from_file_location("catena_admin_service", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


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
