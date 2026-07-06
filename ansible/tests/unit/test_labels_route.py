"""Unit tests for labels_schema.extract_vps_route_labels.

The vps.route.* labels are the client-app ingress host source under the
Dokploy->Portainer migration (Dokploy's domain.by* API is gone). dashboard-
sync + gatus-sync + render.py (App Templates) all read them.

Run: uv run pytest tests/unit/test_labels_route.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "helpers" / "labels_schema.py"
)
_spec = importlib.util.spec_from_file_location("labels_schema", _MOD)
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)


def test_host_only_defaults_port_80():
    got = ls.extract_vps_route_labels("labels:\n  vps.route.host: blog.acme.com\n")
    assert got == {"host": "blog.acme.com", "port": 80}


def test_host_port_service():
    compose = (
        "labels:\n"
        "  vps.route.host: app.acme.com\n"
        "  vps.route.port: 8080\n"
        "  vps.route.service: web\n"
    )
    got = ls.extract_vps_route_labels(compose)
    assert got == {"host": "app.acme.com", "port": 8080, "service": "web"}


def test_quoted_and_equals_forms():
    compose = 'labels:\n  "vps.route.host"="x.acme.com"\n  "vps.route.port"="3000"\n'
    got = ls.extract_vps_route_labels(compose)
    assert got["host"] == "x.acme.com"
    assert got["port"] == 3000


def test_no_host_returns_empty():
    # port/service without a host is not routable -> empty.
    assert ls.extract_vps_route_labels("labels:\n  vps.route.port: 80\n") == {}
    assert ls.extract_vps_route_labels("") == {}
    assert ls.extract_vps_route_labels("services:\n  app:\n    image: x\n") == {}


def test_malformed_port_ignored_host_kept():
    got = ls.extract_vps_route_labels(
        "labels:\n  vps.route.host: h.acme.com\n  vps.route.port: notaport\n"
    )
    assert got == {"host": "h.acme.com", "port": 80}
