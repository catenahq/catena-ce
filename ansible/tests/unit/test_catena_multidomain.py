"""Unit tests for the catena_multidomain filter plugin.

Run: uv run pytest tests/unit/test_catena_multidomain.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "catena_multidomain.py"
)
_spec = importlib.util.spec_from_file_location("catena_multidomain", _PLUGIN)
md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md)


def test_merge_primary_first_and_dedup():
    primary = {"zone": "a.com", "account_id": "acc", "api_token": "tokA"}
    store = [{"zone": "b.com", "account_id": "acc"}, {"zone": "a.com", "account_id": "acc"}]
    tokens = {"b.com": "tokB"}
    out = md.cf_zones_merge_primary(primary, store, tokens)
    assert [z["zone"] for z in out] == ["a.com", "b.com"]  # primary first, dup dropped
    assert out[0]["api_token"] == "tokA"
    assert out[1]["api_token"] == "tokB"


def test_merge_primary_token_from_map_fallback():
    primary = {"zone": "a.com", "account_id": "acc"}  # no inline token
    out = md.cf_zones_merge_primary(primary, [], {"a.com": "fromMap"})
    assert out[0]["api_token"] == "fromMap"


def test_merge_primary_requires_zone():
    import pytest
    with pytest.raises(ValueError):
        md.cf_zones_merge_primary({}, [], {})


def test_strip_tokens_removes_secret():
    zones = [{"zone": "a.com", "account_id": "acc", "api_token": "secret"}]
    out = md.cf_zones_strip_tokens(zones)
    assert out == [{"zone": "a.com", "account_id": "acc"}]
    assert "api_token" not in out[0]


def test_tokens_map_skips_blank():
    zones = [
        {"zone": "a.com", "api_token": "tokA"},
        {"zone": "b.com", "api_token": ""},
        {"zone": "c.com"},
    ]
    assert md.cf_zones_tokens_map(zones) == {"a.com": "tokA"}
