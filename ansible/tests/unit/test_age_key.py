"""Unit tests for helpers/age_key.py parsing (no real age-keygen needed)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
AGE_KEY_PATH = ANSIBLE_DIR / "helpers" / "age_key.py"

_SAMPLE = (
    "# created: 2026-06-06T00:00:00Z\n"
    "# public key: age1examplepublickey00000000000000000000000000000000000000\n"
    "AGE-SECRET-KEY-1EXAMPLESECRETKEY00000000000000000000000000000000000000\n"
)


@pytest.fixture(scope="module")
def age_key():
    spec = importlib.util.spec_from_file_location("age_key_mod", AGE_KEY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_keygen_output_extracts_both_halves(age_key):
    parsed = age_key.parse_keygen_output(_SAMPLE)
    assert parsed.pubkey.startswith("age1example")
    assert parsed.privkey.startswith("AGE-SECRET-KEY-1")


def test_parse_keygen_output_rejects_garbage(age_key):
    with pytest.raises(age_key.AgeKeyError):
        age_key.parse_keygen_output("not a key\n")


def test_extract_secret_finds_privkey_line(age_key):
    assert age_key.extract_secret(_SAMPLE).startswith("AGE-SECRET-KEY-1")


def test_extract_secret_none_when_absent(age_key):
    assert age_key.extract_secret("# only comments\n") is None


def test_keyfile_body_round_trips(age_key):
    key = age_key.AgeKey(
        pubkey="age1pub00000000000000000000000000000000000000000000000000000",
        privkey="AGE-SECRET-KEY-1PRIV0000000000000000000000000000000000000000000",
    )
    body = age_key.keyfile_body(key)
    assert key.pubkey in body
    assert age_key.extract_secret(body) == key.privkey
