"""Unit tests for the dokploy_naming filter plugin.

Focus: dokploy_find_compose, the find-or-create lookup that decides
whether dokploy_compose.yml CREATES a new app or reuses the existing
one. Missing an existing compose mints a duplicate app on re-converge
and orphans the prior container + its fixed host port (the gatus
127.0.0.1:18080 collision that fails backup_rollback / restore /
re-converge). These pin the all-environments, name-or-appName lookup.

Run: uv run pytest tests/unit/test_dokploy_naming.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "dokploy_naming.py"
)
_spec = importlib.util.spec_from_file_location("dokploy_naming", _PLUGIN)
dn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dn)


def _project(*environments):
    return {"environments": list(environments)}


def test_find_compose_matches_by_name_in_first_environment():
    proj = _project({"compose": [{"composeId": "c1", "name": "gatus", "appName": "gatus"}]})
    got = dn.dokploy_find_compose(proj, "gatus")
    assert got is not None and got["composeId"] == "c1"


def test_find_compose_searches_non_first_environment():
    # The env[0]-only lookup used to miss this -> duplicate app + orphan.
    proj = _project(
        {"compose": []},
        {"compose": [{"composeId": "c2", "name": "keycloak", "appName": "keycloak-4s3hgd"}]},
    )
    got = dn.dokploy_find_compose(proj, "keycloak")
    assert got is not None and got["composeId"] == "c2"


def test_find_compose_matches_by_appname():
    proj = _project({"compose": [{"composeId": "c3", "name": "Display Name", "appName": "oauth2-proxy"}]})
    got = dn.dokploy_find_compose(proj, "oauth2-proxy")
    assert got is not None and got["composeId"] == "c3"


def test_find_compose_returns_none_when_absent():
    proj = _project({"compose": [{"composeId": "c4", "name": "gatus", "appName": "gatus"}]})
    assert dn.dokploy_find_compose(proj, "nextcloud") is None


def test_find_compose_tolerates_missing_or_null_fields():
    # First-converge / malformed payloads must not raise -- a None return
    # drives compose.create, which is the safe default.
    assert dn.dokploy_find_compose({}, "gatus") is None
    assert dn.dokploy_find_compose({"environments": None}, "gatus") is None
    assert dn.dokploy_find_compose({"environments": [{"compose": None}]}, "gatus") is None
    assert dn.dokploy_find_compose({"environments": [{}]}, "gatus") is None
    assert dn.dokploy_find_compose(None, "gatus") is None


# --- portainer_container_regex (Portainer <stack>-<svc>-<n>, no hash) -------
import re  # noqa: E402


def test_portainer_regex_defaults_app_index_1():
    rx = dn.portainer_container_regex("gatus")
    assert rx == r"^gatus-app-1$"
    assert re.match(rx, "gatus-app-1")
    assert not re.match(rx, "gatus-app-2")
    # No Dokploy 6-hex hash segment -- the hashed form must NOT match.
    assert not re.match(rx, "gatus-a1b2c3-app-1")


def test_portainer_regex_service_and_index():
    rx = dn.portainer_container_regex("nextcloud", "db", index=2)
    assert re.match(rx, "nextcloud-db-2")
    assert not re.match(rx, "nextcloud-db-1")


def test_portainer_regex_rejects_bad_tokens():
    import pytest
    with pytest.raises(ValueError):
        dn.portainer_container_regex("")
    with pytest.raises(ValueError):
        dn.portainer_container_regex("gatus", "bad name")
    with pytest.raises(ValueError):
        dn.portainer_container_regex("gatus", "app", index=0)
