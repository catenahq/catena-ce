"""Absent input must not produce output that Gatus is then restarted onto.

`build_infra_doc` returned a document with an EMPTY endpoint list when
gatus-infra-spec.json was missing or unreadable. `main` compared that to the
live config, found it different, wrote it, and restarted Gatus. So a missing
spec file DELETED every infrastructure monitor on the host, and the run
printed "wrote ..." and exited 0.

That is the sharpest form of the class: a lane that does damage and reports
success, rather than one that does nothing. Everything downstream agrees with
it -- Gatus shows no failing endpoints, because it is watching nothing.

The spec is rendered by reconcile/roles/infrastructure, so a missing one means the
converge did not run or the file was removed; either way the correct action is
to leave the last known-good config in place and say so.

Run: uv run pytest tests/unit/test_gatus_sync_absent_input.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE_DIR / "scripts" / "gatus-sync.py"


@pytest.fixture(scope="module")
def gs():
    spec = importlib.util.spec_from_file_location("gatus_sync", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gatus_sync"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_a_missing_spec_yields_no_document(gs, tmp_path):
    assert gs.build_infra_doc(tmp_path / "absent.json", {}) is None


def test_an_unreadable_spec_yields_no_document(gs, tmp_path):
    bad = tmp_path / "spec.json"
    bad.write_text("{not json")
    assert gs.build_infra_doc(bad, {}) is None


def test_a_readable_spec_still_renders(gs, tmp_path):
    """The guard must not swallow the working case: an empty LIST is a real
    spec saying there are no infra endpoints, which is different from no spec
    at all."""
    good = tmp_path / "spec.json"
    good.write_text(json.dumps([
        {"label": "Portainer", "url": "http://x:9000/", "group": "Infra"},
    ]))
    doc = gs.build_infra_doc(good, {})
    assert doc is not None
    assert "Portainer" in doc

    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert gs.build_infra_doc(empty, {}) == gs.build_infra_doc(empty, {})
    assert gs.build_infra_doc(empty, {}) is not None


def test_main_leaves_the_live_config_alone_when_the_spec_is_gone(gs, tmp_path, monkeypatch):
    """The end-to-end property: main must not write, and must not restart."""
    live = tmp_path / "40-infra.yaml"
    live.write_text("endpoints:\n  - name: Portainer\n")
    before = live.read_text()

    monkeypatch.setenv("GATUS_INFRA_SPEC_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("GATUS_INFRA_CONFIG_PATH", str(live))
    monkeypatch.setenv("GATUS_CONFIG_PATH", str(tmp_path / "50-apps.yaml"))
    monkeypatch.setenv("GATUS_CONTAINER_NAME_RE", "gatus")
    monkeypatch.setenv("INFRA_COMPOSE_NAMES", "gatus")
    monkeypatch.setenv("AUTH_HOSTNAME", "auth.example.test")
    monkeypatch.setenv("HEALTHCHECKS_SUMMARY_PATH", "")
    monkeypatch.setenv("HEALTHCHECKS_API_URL", "")
    monkeypatch.setenv("VERSION_CHECK_PATH", "")
    monkeypatch.setenv("GATUS_SUMMARY_PATH", "")
    monkeypatch.setattr(gs, "list_routed_containers", lambda *a, **k: [])
    monkeypatch.setattr(gs, "load_display_name_overrides", lambda *a, **k: {})
    monkeypatch.setattr(gs, "find_gatus_container", lambda *a, **k: None)

    restarted = []
    monkeypatch.setattr(gs, "restart_gatus", lambda c: restarted.append(c))

    gs.main()

    assert live.read_text() == before, "the live infra config must be untouched"
    assert restarted == [], "Gatus must not be restarted onto a config we did not write"


def test_the_orphan_prune_skip_names_its_consequence():
    """An orphan is a check whose endpoint is gone; unpaused it goes late and
    PAGES forever for a service nobody deployed any more. Skipping the prune
    silently made that look like a real outage."""
    body = SCRIPT.read_text()
    assert "orphaned checks were NOT paused" in body
    assert "keep paging" in body
