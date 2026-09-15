"""The served-domain projection reader.

reconcile/roles/keycloak routes one auth.<zone> host rule per attached domain, and gets
the list from the projection the Cloudflare tunnel engine writes after a
successful converge. The safety property under test is one-directional: every
reason the file cannot be trusted must produce an EMPTY list, so the routing
falls back to the primary domain rather than advertising a hostname the edge
does not serve.

Run: uv run pytest tests/unit/test_catena_served_zones.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_FILTERS = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins"
)
sys.path.insert(0, str(_FILTERS))

import catena_served_zones as csz  # noqa: E402


NOW = "2026-07-25T12:00:00Z"


def _projection(zones, generated_at="2026-07-25T11:30:00Z", schema=1):
    return json.dumps({
        "schema": schema,
        "generated_at": generated_at,
        "zones": [{"zone": z, "account_id": "acc"} for z in zones],
    })


def test_returns_secondaries_in_order_dropping_the_primary():
    raw = _projection(["primary.com", "second.com", "third.com"])
    assert csz.catena_served_zones(raw, now=NOW) == ["second.com", "third.com"]


def test_single_domain_host_has_no_secondaries():
    assert csz.catena_served_zones(_projection(["only.com"]), now=NOW) == []


def test_accepts_bytes_from_slurp():
    raw = _projection(["primary.com", "second.com"]).encode()
    assert csz.catena_served_zones(raw, now=NOW) == ["second.com"]


def test_duplicates_collapse():
    raw = _projection(["primary.com", "second.com", "second.com"])
    assert csz.catena_served_zones(raw, now=NOW) == ["second.com"]


def test_blank_and_absent_input_is_primary_only():
    for raw in ("", "   ", None, b""):
        assert csz.catena_served_zones(raw, now=NOW) == []


def test_malformed_input_is_primary_only():
    for raw in ("not json", "{", '{"zones": "nope"}', '{"schema":1}'):
        assert csz.catena_served_zones(raw, now=NOW) == []


def test_unknown_schema_is_refused():
    raw = _projection(["primary.com", "second.com"], schema=99)
    assert csz.catena_served_zones(raw, now=NOW) == []


def test_legacy_bare_array_is_refused():
    """The shape this replaced carried no stamp, so a stale copy could not be
    told from a current one. Accepting it would reintroduce exactly that."""
    raw = json.dumps([{"zone": "primary.com"}, {"zone": "second.com"}])
    assert csz.catena_served_zones(raw, now=NOW) == []


def test_stale_projection_is_refused():
    raw = _projection(["primary.com", "second.com"],
                      generated_at="2026-07-20T12:00:00Z")
    assert csz.catena_served_zones(raw, now=NOW, max_age_hours=48) == []
    # Inside the bound it is trusted.
    assert csz.catena_served_zones(raw, now=NOW, max_age_hours=200) == ["second.com"]


def test_missing_stamp_is_refused_when_a_bound_is_set():
    raw = json.dumps({"schema": 1, "zones": [{"zone": "a.com"}, {"zone": "b.com"}]})
    assert csz.catena_served_zones(raw, now=NOW, max_age_hours=48) == []


def test_no_bound_disables_the_staleness_check():
    raw = _projection(["primary.com", "second.com"],
                      generated_at="2020-01-01T00:00:00Z")
    assert csz.catena_served_zones(raw, now=NOW, max_age_hours=0) == ["second.com"]


def test_unknown_now_does_not_refuse_a_stamped_projection():
    """ansible_date_time can be missing on a fact-less run. Without a reference
    time the age is unknowable; a stamped projection is still taken, because the
    alternative is dropping every secondary domain on a run that simply did not
    gather facts."""
    raw = _projection(["primary.com", "second.com"],
                      generated_at="2020-01-01T00:00:00Z")
    assert csz.catena_served_zones(raw, now="", max_age_hours=48) == ["second.com"]


def test_non_dict_entries_are_skipped():
    raw = json.dumps({
        "schema": 1,
        "generated_at": "2026-07-25T11:30:00Z",
        "zones": [{"zone": "primary.com"}, "junk", {"zone": ""}, {"zone": "ok.com"}],
    })
    assert csz.catena_served_zones(raw, now=NOW) == ["ok.com"]


def test_filter_is_registered():
    assert "catena_served_zones" in csz.FilterModule().filters()
