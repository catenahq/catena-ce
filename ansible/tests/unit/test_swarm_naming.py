"""Unit tests for the swarm_naming filter plugin.

Focus: swarm_container_regex, the anchored regex tasks use to find a running
task container by (stack, service, slot) in `docker ps` output. `docker stack
deploy` creates services named `<stack>_<service>` and swarm names their task
containers `<stack>_<service>.<slot>.<task-id>`.

The Portainer form `<stack>-<service>-<index>` must NOT match. A host
converged by an older build still has those containers, and matching them
would let a task operate on the stack it just replaced.

Run: uv run pytest tests/unit/test_swarm_naming.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "swarm_naming.py"
)
_spec = importlib.util.spec_from_file_location("swarm_naming", _PLUGIN)
sn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sn)


def test_defaults_app_slot_1():
    rx = sn.swarm_container_regex("gatus")
    assert re.match(rx, "gatus_app.1.k3m9xq2wz7v1p0abcdefghijk")
    assert not re.match(rx, "gatus_app.2.k3m9xq2wz7v1p0abcdefghijk")
    # The task id is mandatory: a bare service name is the SERVICE, not a
    # container, and a task that ran `docker exec` against it would fail.
    assert not re.match(rx, "gatus_app")
    assert not re.match(rx, "gatus_app.1")


def test_portainer_form_does_not_match():
    rx = sn.swarm_container_regex("gatus")
    assert not re.match(rx, "gatus-app-1")
    assert not re.match(rx, "gatus-a1b2c3-app-1")


def test_service_and_slot():
    rx = sn.swarm_container_regex("nextcloud", "db", slot=2)
    assert re.match(rx, "nextcloud_db.2.abc123")
    assert not re.match(rx, "nextcloud_db.1.abc123")


def test_dashes_in_stack_and_service_survive():
    # oauth2-proxy deploys one service per gated app: the stack AND the
    # service key both carry dashes, and the underscore before the service
    # is the only separator that stays unambiguous.
    rx = sn.swarm_container_regex("oauth2-proxy", "oauth2-proxy-vps-docs")
    assert re.match(rx, "oauth2-proxy_oauth2-proxy-vps-docs.1.zz99")
    assert not re.match(rx, "oauth2-proxy_oauth2-proxy-other.1.zz99")


def test_rejects_bad_tokens():
    with pytest.raises(ValueError):
        sn.swarm_container_regex("")
    with pytest.raises(ValueError):
        sn.swarm_container_regex("gatus", "bad name")
    with pytest.raises(ValueError):
        sn.swarm_container_regex("gatus", "app", slot=0)
