"""Unit tests for portainer_api.resolve_compose_env + iter_stacks env resolution.

Portainer keeps a stack's compose file verbatim (with `${...}` refs) and its
resolved values in a separate Env array. dashboard-sync reads the stored file,
so a client app's vps.route.host=${DOMAIN_HOST} must be resolved against the
stack Env before gate_routes extracts the host -- otherwise the Traefik rule
becomes a literal Host(${DOMAIN_HOST}) and the app 404s.

Run: uv run pytest tests/unit/test_portainer_env_resolve.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "scripts" / "portainer_api.py"
)
_spec = importlib.util.spec_from_file_location("portainer_api", _MOD)
da = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(da)


def test_resolves_plain_ref():
    assert da.resolve_compose_env(
        "vps.route.host=${DOMAIN_HOST}", {"DOMAIN_HOST": "book.acme.com"}
    ) == "vps.route.host=book.acme.com"


def test_default_used_when_unset():
    assert da.resolve_compose_env("${PORT:-80}", {}) == "80"
    assert da.resolve_compose_env("${PORT-80}", {}) == "80"


def test_set_wins_over_default():
    assert da.resolve_compose_env("${PORT:-80}", {"PORT": "9000"}) == "9000"


def test_unset_no_default_left_literal():
    # A downstream gap must stay visible; never silently blank a host.
    assert da.resolve_compose_env("${DOMAIN_HOST}", {}) == "${DOMAIN_HOST}"


def test_double_dollar_is_literal():
    assert da.resolve_compose_env("price is $${AMOUNT}", {"AMOUNT": "5"}) == "price is ${AMOUNT}"


def test_empty_and_none_passthrough():
    assert da.resolve_compose_env("", {"X": "y"}) == ""
    assert da.resolve_compose_env(None, {}) is None


def test_iter_stacks_resolves_route_host(monkeypatch):
    stacks = [
        {
            "Id": 7,
            "Name": "easyappointments",
            "Status": 1,
            "Env": [{"name": "DOMAIN_HOST", "value": "book.acme.com"}],
        },
        {"Id": 8, "Name": "inactive", "Status": 2, "Env": []},
    ]
    files = {
        7: 'services:\n  app:\n    labels:\n      - "vps.route.host=${DOMAIN_HOST}"\n',
    }
    monkeypatch.setattr(da, "list_stacks", lambda *a, **k: stacks)
    monkeypatch.setattr(da, "stack_file", lambda base, key, sid: files.get(sid, ""))

    out = list(da.iter_stacks("http://x/api", "k", skip_names=set()))
    assert len(out) == 1  # inactive dropped
    name, sid, body = out[0]
    assert name == "easyappointments"
    assert "vps.route.host=book.acme.com" in body
    assert "${DOMAIN_HOST}" not in body


def test_iter_stacks_skips_infra_names(monkeypatch):
    stacks = [{"Id": 1, "Name": "traefik", "Status": 1, "Env": []}]
    monkeypatch.setattr(da, "list_stacks", lambda *a, **k: stacks)
    monkeypatch.setattr(da, "stack_file", lambda *a, **k: "")
    assert list(da.iter_stacks("http://x/api", "k", skip_names={"traefik"})) == []
