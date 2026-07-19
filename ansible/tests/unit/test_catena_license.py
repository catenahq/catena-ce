"""Unit tests for the catena_license_active filter plugin -- the offline
license gate the EE multi-domain overlay uses to lift the single-domain cap.

Run: uv run pytest tests/unit/test_catena_license.py
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "playbooks" / "filter_plugins" / "catena_license.py"
)
_spec = importlib.util.spec_from_file_location("catena_license", _PLUGIN)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

active = cl.catena_license_active


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _mint(priv: Ed25519PrivateKey, *, edition: str, valid_until: str) -> str:
    payload = json.dumps({
        "sub": "acct-1", "edition": edition,
        "iat": "2026-01-01T00:00:00Z", "valid_until": valid_until,
    }).encode("utf-8")
    sig = priv.sign(payload)
    return _b64url(payload) + "." + _b64url(sig)


def _pubkey_b64(priv: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def test_valid_business_within_window_is_active():
    priv = Ed25519PrivateKey.generate()
    tok = _mint(priv, edition="business", valid_until="2027-01-01T00:00:00Z")
    assert active(tok, _pubkey_b64(priv), now_iso="2026-06-01T00:00:00Z") is True


def test_expired_past_grace_is_inactive():
    priv = Ed25519PrivateKey.generate()
    tok = _mint(priv, edition="business", valid_until="2026-01-01T00:00:00Z")
    # now is a week past valid_until, well beyond the 72h grace.
    assert active(tok, _pubkey_b64(priv), now_iso="2026-01-08T00:00:00Z") is False


def test_within_grace_is_still_active():
    priv = Ed25519PrivateKey.generate()
    tok = _mint(priv, edition="business", valid_until="2026-01-01T00:00:00Z")
    # 24h past valid_until, inside the 72h grace window.
    assert active(tok, _pubkey_b64(priv), now_iso="2026-01-02T00:00:00Z") is True


def test_community_edition_is_never_active():
    priv = Ed25519PrivateKey.generate()
    tok = _mint(priv, edition="community", valid_until="2027-01-01T00:00:00Z")
    assert active(tok, _pubkey_b64(priv), now_iso="2026-06-01T00:00:00Z") is False


def test_tampered_signature_is_inactive():
    priv = Ed25519PrivateKey.generate()
    tok = _mint(priv, edition="business", valid_until="2027-01-01T00:00:00Z")
    head, _, sig = tok.partition(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert active(head + "." + flipped, _pubkey_b64(priv),
                  now_iso="2026-06-01T00:00:00Z") is False


def test_wrong_pubkey_is_inactive():
    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    tok = _mint(priv, edition="business", valid_until="2027-01-01T00:00:00Z")
    assert active(tok, _pubkey_b64(other), now_iso="2026-06-01T00:00:00Z") is False


def test_malformed_and_empty_never_raise():
    priv = Ed25519PrivateKey.generate()
    pk = _pubkey_b64(priv)
    for bad in ("", "no-dot", "a.b.c", "...", "@@@.@@@"):
        assert active(bad, pk) is False
    assert active(None, pk) is False
    assert active("a.b", None) is False
