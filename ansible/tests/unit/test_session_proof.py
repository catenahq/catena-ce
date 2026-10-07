"""helpers/session_proof.py: the install hardens sshd only after the session
running it logged in as ops with a key ops accepts.

Run: uv run pytest tests/unit/test_session_proof.py
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import session_proof as sp  # noqa: E402

BLOB_A = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"a" * 36).decode()
BLOB_B = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"b" * 36).decode()
FP_A = "SHA256:" + base64.b64encode(
    hashlib.sha256(base64.b64decode(BLOB_A)).digest()).decode().rstrip("=")
FP_B = "SHA256:" + base64.b64encode(
    hashlib.sha256(base64.b64decode(BLOB_B)).digest()).decode().rstrip("=")
SESSION = "203.0.113.5 51234 198.51.100.7 22"
AUTHORIZED = f"ssh-ed25519 {BLOB_A} catena\n"


def _journal(user: str = "ops", addr: str = "203.0.113.5", port: str = "51234",
             fp: str = FP_A) -> str:
    return (f"Server listening on 0.0.0.0 port 22.\n"
            f"Accepted publickey for {user} from {addr} port {port} ssh2: "
            f"ED25519 {fp}\n"
            f"pam_unix(sshd:session): session opened for user {user}\n")


def test_the_fingerprint_is_openssh_s():
    assert sp.fingerprint(BLOB_A) == FP_A


def test_a_key_login_as_ops_with_an_authorized_key_proves_the_session():
    ok, why = sp.prove("ops", SESSION, _journal(), AUTHORIZED)
    assert ok, why


def test_a_login_from_another_connection_proves_nothing():
    """Another session (the provider login, or ops from elsewhere) is not
    the one about to harden sshd."""
    ok, _ = sp.prove("ops", SESSION, _journal(port="40000"), AUTHORIZED)
    assert not ok
    ok, _ = sp.prove("ops", SESSION, _journal(addr="192.0.2.9"), AUTHORIZED)
    assert not ok


def test_a_session_as_another_account_proves_nothing():
    ok, why = sp.prove("ops", SESSION, _journal(user="debian"), AUTHORIZED)
    assert not ok and "did not log in as ops" in why


def test_a_password_login_proves_nothing():
    journal = ("Accepted password for ops from 203.0.113.5 port 51234 ssh2\n")
    ok, _ = sp.prove("ops", SESSION, journal, AUTHORIZED)
    assert not ok


def test_a_key_ops_does_not_hold_proves_nothing():
    ok, why = sp.prove("ops", SESSION, _journal(fp=FP_B), AUTHORIZED)
    assert not ok and "not in ops's authorized_keys" in why


def test_keys_behind_options_are_read():
    """The panel account's keys carry quoted options with spaces in them."""
    text = (f'restrict,port-forwarding,permitopen="127.0.0.1:9010",'
            f'permitopen="127.0.0.1:9000" ssh-ed25519 {BLOB_B} catena\n'
            f'# a comment\n\nssh-ed25519 {BLOB_A}\n')
    assert sp.authorized_fingerprints(text) == {FP_A, FP_B}


def test_no_ssh_connection_is_no_session():
    ok, why = sp.prove("ops", "", _journal(), AUTHORIZED)
    assert not ok and "no SSH session" in why
