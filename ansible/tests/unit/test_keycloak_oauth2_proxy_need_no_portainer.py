"""The keycloak and oauth2_proxy roles deploy swarm stacks and never call
Portainer, so neither waits on the Portainer API key: a host whose Portainer is
down still converges its identity server and sign-in gates.

Run: uv run pytest tests/unit/test_keycloak_oauth2_proxy_need_no_portainer.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROLES = Path(__file__).resolve().parents[2] / "reconcile" / "roles"
_MAINS = [_ROLES / "keycloak" / "tasks" / "main.yml",
          _ROLES / "oauth2_proxy" / "tasks" / "main.yml"]


@pytest.mark.parametrize("path", _MAINS, ids=lambda p: p.parent.parent.name)
def test_no_task_reads_the_portainer_key(path):
    assert "portainer" not in path.read_text().lower()


@pytest.mark.parametrize("path", _MAINS, ids=lambda p: p.parent.parent.name)
def test_no_task_is_conditioned_on_the_key(path):
    def walk(tasks):
        for t in tasks or []:
            yield t
            for key in ("block", "rescue", "always"):
                yield from walk(t.get(key))

    for task in walk(yaml.safe_load(path.read_text())):
        assert "portainer_api_key" not in str(task.get("when", "")), task.get("name")
