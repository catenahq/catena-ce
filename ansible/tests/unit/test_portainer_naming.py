"""Unit tests for the portainer_naming filter plugin.

Focus: portainer_container_regex, the anchored regex tasks use to find a
running container by (stack, service, index) in `docker ps` output. Portainer
names containers `<stack>-<service>-<index>` (no per-instance hash, unlike
Dokploy) -- the hashed form must NOT match.

Run: uv run pytest tests/unit/test_portainer_naming.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "portainer_naming.py"
)
_spec = importlib.util.spec_from_file_location("portainer_naming", _PLUGIN)
pn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pn)


def test_defaults_app_index_1():
    rx = pn.portainer_container_regex("gatus")
    assert rx == r"^gatus-app-1$"
    assert re.match(rx, "gatus-app-1")
    assert not re.match(rx, "gatus-app-2")
    # No Dokploy 6-hex hash segment -- the hashed form must NOT match.
    assert not re.match(rx, "gatus-a1b2c3-app-1")


def test_service_and_index():
    rx = pn.portainer_container_regex("nextcloud", "db", index=2)
    assert re.match(rx, "nextcloud-db-2")
    assert not re.match(rx, "nextcloud-db-1")


def test_rejects_bad_tokens():
    with pytest.raises(ValueError):
        pn.portainer_container_regex("")
    with pytest.raises(ValueError):
        pn.portainer_container_regex("gatus", "bad name")
    with pytest.raises(ValueError):
        pn.portainer_container_regex("gatus", "app", index=0)
