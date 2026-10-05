"""helpers/host_key.py: a server that offers none of the keys this machine
trusts for it presents a changed key, and forgetting drops what was trusted.

The trusted side runs the real ssh-keygen against a known_hosts file in the
test's directory; the server's side is a stubbed keyscan.

Run: uv run pytest tests/unit/test_host_key.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import host_key  # noqa: E402

HOST = "203.0.113.10"


def _key(tmp_path: Path, name: str) -> str:
    """`type base64` of a fresh ed25519 public key."""
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                    str(tmp_path / name)], check=True)
    return " ".join((tmp_path / f"{name}.pub").read_text().split()[:2])


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A known_hosts that trusts key `old` for HOST, and a server that offers
    whatever the test puts in `offers`."""
    keys = {name: _key(tmp_path, name) for name in ("old", "new")}
    known = tmp_path / "known_hosts"
    known.write_text(f"{HOST} {keys['old']}\n[{HOST}]:2222 {keys['old']}\n")
    offers: list[str] = []
    monkeypatch.setattr(host_key, "_offered", lambda host, port: "".join(
        f"{host_key.known_name(host, port)} {keys[name]}\n" for name in offers))
    return known, keys, offers


def test_a_server_offering_the_trusted_key_has_not_changed(server):
    known, _, offers = server
    offers[:] = ["old", "new"]
    assert host_key.changed(HOST, 22, known) == ([], [])


def test_a_server_offering_only_another_key_has_changed(server):
    known, keys, offers = server
    offers[:] = ["new"]
    trusted, offered = host_key.changed(HOST, 22, known)
    assert len(trusted) == 1 and len(offered) == 1 and trusted != offered
    fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", "-"], input=f"{HOST} {keys['new']}\n",
        capture_output=True, text=True, check=True).stdout.split()[1]
    assert offered == [f"ssh-ed25519 {fingerprint}"]


def test_nothing_trusted_or_nothing_answering_is_no_change(server, tmp_path):
    known, _, offers = server
    offers[:] = ["new"]
    assert host_key.changed("198.51.100.7", 22, known) == ([], [])
    assert host_key.changed(HOST, 22, tmp_path / "absent") == ([], [])
    offers[:] = []
    assert host_key.changed(HOST, 22, known) == ([], [])


def test_another_port_is_its_own_entry(server):
    """ssh files a server on another port as [host]:port."""
    known, _, offers = server
    offers[:] = ["new"]
    assert host_key.known_name(HOST, 2222) == f"[{HOST}]:2222"
    assert host_key.changed(HOST, 2222, known)[0]


def test_forget_drops_the_server_s_keys_and_no_others(server):
    known, _, offers = server
    offers[:] = ["new"]
    host_key.forget(HOST, 22, known)
    assert host_key.changed(HOST, 22, known) == ([], [])
    assert host_key.changed(HOST, 2222, known)[0]
