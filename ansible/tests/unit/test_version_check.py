"""Unit tests for the auto-detecting Community version-check.

Covers image-ref parsing, upstream derivation from the ref, same-shape
tag matching, and an end-to-end build_report with docker + upstream
mocked. The whole point is auto-detection with no service registry, so
the report tests assert that running images alone drive the rows."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "catena-version-check.py"


@pytest.fixture()
def vc():
    spec = importlib.util.spec_from_file_location("version_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- parse_image ------------------------------------------------------------
@pytest.mark.parametrize("image,expected", [
    ("nextcloud:31-apache", ("docker.io", "library/nextcloud", "31-apache")),
    ("espocrm/espocrm:9.2.0", ("docker.io", "espocrm/espocrm", "9.2.0")),
    ("ghcr.io/immich-app/immich-server:v1.119.0",
     ("ghcr.io", "immich-app/immich-server", "v1.119.0")),
    ("quay.io/keycloak/keycloak:26.5.7",
     ("quay.io", "keycloak/keycloak", "26.5.7")),
    ("cloudflare/cloudflared:2026.6.0",
     ("docker.io", "cloudflare/cloudflared", "2026.6.0")),
    ("postgres:16-alpine@sha256:deadbeef",
     ("docker.io", "library/postgres", "16-alpine")),
    ("registry:5000/team/app:1.2.3", ("registry:5000", "team/app", "1.2.3")),
])
def test_parse_image(vc, image, expected):
    assert vc.parse_image(image) == expected


# --- derive_upstream --------------------------------------------------------
def test_derive_upstream_sources(vc):
    assert vc.derive_upstream("ghcr.io", "immich-app/immich")["source"] == "github"
    assert vc.derive_upstream("quay.io", "keycloak/keycloak")["source"] == "quay"
    assert vc.derive_upstream("docker.io", "library/nextcloud")["source"] == "dockerhub"
    # A private/unknown registry can't be queried.
    assert vc.derive_upstream("registry:5000", "team/app") is None


def test_derive_upstream_dockerhub_official_url(vc):
    up = vc.derive_upstream("docker.io", "library/postgres")
    assert "hub.docker.com/_/postgres" in up["url"]


# --- tag shape matching -----------------------------------------------------
def test_tag_pattern_keeps_suffix(vc):
    pat = vc.tag_pattern_for("16-alpine")
    import re
    assert re.fullmatch(pat, "16.3-alpine")
    assert not re.fullmatch(pat, "16.3")          # bare must not match -alpine pin
    assert vc.tag_pattern_for("latest") == ""     # floating -> no comparison


def test_is_up_to_date(vc):
    assert vc.is_up_to_date("v0.29.0", "v0.29.0")
    assert vc.is_up_to_date("2026.2", "2026.2.2")   # minor pin, patch in series
    assert not vc.is_up_to_date("v0.29.0", "v0.30.0")


# --- end-to-end build_report (docker + upstream mocked) ---------------------
def _isolate_managed_state(vc, monkeypatch, tmp_path):
    """Point the Business updater-state paths at nonexistent files so the
    report renders in its CE (no-license) shape during tests."""
    for attr in ("MANAGED_STATE_FILE", "MANAGED_FAILED_FILE", "MANAGED_PAUSE_FLAG"):
        monkeypatch.setattr(vc, attr, str(tmp_path / f"{attr}.absent"))


def test_build_report_autodetects_and_flags(vc, monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "OVERRIDES_FILE", str(tmp_path / "none.json"))
    _isolate_managed_state(vc, monkeypatch, tmp_path)
    # Two running services discovered straight from docker -- no registry.
    monkeypatch.setattr(vc, "discover_running_services", lambda: [
        {"name": "keycloak", "image": "quay.io/keycloak/keycloak:26.5.0",
         "display_override": ""},
        {"name": "cloudflared", "image": "cloudflare/cloudflared:2026.6.0",
         "display_override": ""},
    ])
    monkeypatch.setattr(vc, "quay_latest_matching", lambda repo, pat: "26.5.7")
    monkeypatch.setattr(vc, "dockerhub_latest_matching", lambda repo, pat: "2026.6.0")

    rep = vc.build_report()
    by_name = {s["name"]: s for s in rep["services"]}
    assert by_name["keycloak"]["status"] == "outdated"
    assert by_name["keycloak"]["latest"] == "26.5.7"
    assert by_name["cloudflared"]["status"] == "up-to-date"
    assert rep["outdated_count"] == 1
    assert rep["top_outdated_name"] == "keycloak"


def test_build_report_excludes_via_override(vc, monkeypatch, tmp_path):
    ov = tmp_path / "ov.json"
    ov.write_text('{"library/postgres": {"exclude": true}}')
    monkeypatch.setattr(vc, "OVERRIDES_FILE", str(ov))
    _isolate_managed_state(vc, monkeypatch, tmp_path)
    monkeypatch.setattr(vc, "discover_running_services", lambda: [
        {"name": "postgres", "image": "postgres:16-alpine", "display_override": ""},
    ])
    rep = vc.build_report()
    assert rep["services"] == []
