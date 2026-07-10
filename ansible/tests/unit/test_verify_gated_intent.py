"""Unit tests for scripts/verify_gated_intent.py + gate_routes.derive_route_intent.

The 1ad rework: validate derives the expected gated set from the SAME
Portainer-label walk the route writer uses, instead of enumerating
route files (which went green when a gate file was stripped). These
tests drive the pure pieces (intent derivation via a stubbed
iter_stacks, classify/findings via synthetic reports) with no
Portainer and no network.

Run: uv run pytest tests/unit/test_verify_gated_intent.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ANSIBLE = Path(__file__).resolve().parents[3] / "ansible"
_SCRIPTS = _ANSIBLE / "scripts"
_HELPERS = _ANSIBLE / "helpers"


def _load(name: str, fname: str):
    # The scripts sys.path-insert their own dir and import siblings by
    # bare name (labels_schema ships flat beside them on the host), so
    # make both the scripts dir and helpers/ importable first.
    for d in (str(_SCRIPTS), str(_HELPERS)):
        if d not in sys.path:
            sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate_routes = _load("gate_routes", "gate_routes.py")
vgi = _load("verify_gated_intent", "verify_gated_intent.py")


_GATED_COMPOSE = (
    "services:\n"
    "  app:\n"
    "    image: x\n"
    "    labels:\n"
    "      vps.route.host: crm.acme.test\n"
    "      vps.auth.mode: private\n"
    "    networks:\n"
    "      catena-network:\n"
    "        aliases: [crm]\n"
)
_PUBLIC_COMPOSE = (
    "services:\n"
    "  app:\n"
    "    image: x\n"
    "    labels:\n"
    "      vps.route.host: blog.acme.test\n"
    "      vps.auth.mode: public\n"
    "    networks:\n"
    "      catena-network:\n"
    "        aliases: [blog]\n"
)
_NO_ROUTE_COMPOSE = "services:\n  app:\n    image: x\n"


def _stub_iter_stacks(stacks):
    def iter_stacks(api_base, api_key, skip_names, seen=None):
        for name, body in stacks:
            yield name, 1, body
    return iter_stacks


def _derive(monkeypatch, stacks):
    monkeypatch.setattr(
        gate_routes.dokploy_api, "iter_stacks", _stub_iter_stacks(stacks),
    )
    return list(gate_routes.derive_route_intent("http://x", "k", set(), "auth.acme.test"))


# --- derive_route_intent ----------------------------------------------------
def test_derive_splits_public_and_gated(monkeypatch):
    intents = _derive(monkeypatch, [("crm", _GATED_COMPOSE), ("blog", _PUBLIC_COMPOSE)])
    by_name = {i["name"]: i for i in intents}
    assert by_name["crm"]["is_public"] is False
    assert by_name["crm"]["host"] == "crm.acme.test"
    assert by_name["crm"]["fname"].endswith("-auto-gate.yml")
    assert by_name["blog"]["is_public"] is True


def test_derive_skips_unrouted_and_auth_host(monkeypatch):
    auth_compose = _GATED_COMPOSE.replace("crm.acme.test", "auth.acme.test")
    intents = _derive(
        monkeypatch,
        [("plain", _NO_ROUTE_COMPOSE), ("idp", auth_compose)],
    )
    assert intents == []


def test_derive_unlabeled_app_defaults_gated(monkeypatch):
    # No vps.auth.mode -> DEFAULT-DENY (admin-only), never public.
    compose = _GATED_COMPOSE.replace("      vps.auth.mode: private\n", "")
    intents = _derive(monkeypatch, [("crm", compose)])
    assert len(intents) == 1
    assert intents[0]["is_public"] is False


# --- classify ---------------------------------------------------------------
def _intent(name, host, public=False, fname=None):
    return {
        "name": name, "host": host, "is_public": public,
        "allowed": [], "slug": name, "backend_alias": name,
        "route_slug": name, "port": 80,
        "fname": fname or f"{name}-auto-gate.yml",
    }


def test_classify_missing_route_file_is_a_finding(tmp_path):
    report = vgi.classify(
        [_intent("crm", "crm.acme.test")], tmp_path, {"crm.acme.test": 302},
    )
    assert report["missing_route_file"] == [
        {"app": "crm", "host": "crm.acme.test", "fname": "crm-auto-gate.yml"}
    ]
    assert vgi.findings(report)  # the stripped-gate regression is FATAL


def test_classify_bypass_and_healthy(tmp_path):
    (tmp_path / "crm-auto-gate.yml").write_text("x")
    (tmp_path / "wiki-auto-gate.yml").write_text("x")
    report = vgi.classify(
        [_intent("crm", "crm.acme.test"), _intent("wiki", "wiki.acme.test")],
        tmp_path,
        {"crm.acme.test": 200, "wiki.acme.test": 302},
    )
    assert report["bypass"] == ["crm.acme.test"]
    assert report["healthy"] == ["wiki.acme.test"]
    assert any("auth bypass" in f for f in vgi.findings(report))


def test_classify_unreachable_fails_closed(tmp_path):
    (tmp_path / "crm-auto-gate.yml").write_text("x")
    report = vgi.classify(
        [_intent("crm", "crm.acme.test")], tmp_path,
        {"crm.acme.test": "URLError: timeout"},
    )
    assert report["unreachable"] == [
        {"host": "crm.acme.test", "status": "URLError: timeout"}
    ]
    assert any("fail-closed" in f for f in vgi.findings(report))


def test_classify_odd_status_fails_closed(tmp_path):
    (tmp_path / "crm-auto-gate.yml").write_text("x")
    report = vgi.classify(
        [_intent("crm", "crm.acme.test")], tmp_path, {"crm.acme.test": 404},
    )
    assert report["unreachable"] == [{"host": "crm.acme.test", "status": 404}]


def test_classify_public_host_is_exempt(tmp_path):
    (tmp_path / "blog-auto-gate.yml").write_text("x")
    report = vgi.classify(
        [_intent("blog", "blog.acme.test", public=True)], tmp_path, {},
    )
    assert report["public"] == ["blog.acme.test"]
    assert not vgi.findings(report)


def test_classify_stale_route_file_is_a_finding(tmp_path):
    (tmp_path / "ghost-auto-gate.yml").write_text("x")
    report = vgi.classify([], tmp_path, {})
    assert report["stale_route_file"] == ["ghost-auto-gate.yml"]
    assert any("stale route file" in f for f in vgi.findings(report))


def test_classify_all_clear(tmp_path):
    (tmp_path / "crm-auto-gate.yml").write_text("x")
    report = vgi.classify(
        [_intent("crm", "crm.acme.test")], tmp_path, {"crm.acme.test": 401},
    )
    assert not vgi.findings(report)
