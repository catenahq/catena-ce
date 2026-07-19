"""Ansible filter: offline verification of a Catena license token.

This is the VERIFY side of the license contract only (CP2/CP3: minting +
signing live in catena-ee; verification is public). It mirrors the Go
`license.Verify` + `license.Active` wire format so an ansible converge can
gate a Business feature on an active license the same way the catena-admin
shell's registry does, without reaching the license endpoint.

Token wire format (identical to license/license.go):
    base64url(payload) "." base64url(signature)
where payload = JSON({sub, edition, iat, valid_until}) signed with the
operator ed25519 private key. The public key is base64-std ed25519.

`catena_license_active(token, pubkey_b64, grace_hours=72, now_iso=None)`
returns True iff the token verifies AND its edition is "business" AND now is
within valid_until + grace. Any malformed / forged / non-business / lapsed
token returns False -- never raises -- so a converge DEGRADES to Community
rather than failing closed on a bad or missing license (LE2/LE3).

End-to-end coverage:
    ansible/tests/unit/test_catena_license.py
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone


def _b64url_decode(s):
    """Decode base64url without padding (Go's RawURLEncoding)."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _parse_ts(value):
    """Parse an RFC3339 timestamp (with a trailing Z or an offset) to an
    aware UTC datetime. Returns None on anything unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def catena_license_active(token, pubkey_b64, grace_hours=72, now_iso=None):
    """True iff token is a valid, active Business license. Never raises."""
    # Ed25519 verification needs the cryptography lib (already a converge
    # dependency via community.crypto/sops). Import lazily so a controller
    # without it fails soft (no license -> Community) rather than at import.
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        return False

    if not isinstance(token, str) or not isinstance(pubkey_b64, str):
        return False
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    try:
        payload = _b64url_decode(parts[0])
        sig = _b64url_decode(parts[1])
        pub_raw = base64.b64decode(pubkey_b64.strip())
    except (binascii.Error, ValueError):
        return False
    if len(pub_raw) != 32:
        return False

    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, payload)
    except (InvalidSignature, ValueError):
        return False

    try:
        claims = json.loads(payload)
    except (ValueError, TypeError):
        return False
    if not isinstance(claims, dict) or claims.get("edition") != "business":
        return False

    valid_until = _parse_ts(claims.get("valid_until"))
    if valid_until is None:
        return False
    now = _parse_ts(now_iso) if now_iso else datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        grace = timedelta(hours=float(grace_hours))
    except (TypeError, ValueError):
        grace = timedelta(hours=72)
    return now <= valid_until + grace


class FilterModule:
    def filters(self):
        return {"catena_license_active": catena_license_active}
