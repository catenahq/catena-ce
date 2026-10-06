"""helpers/public_https.py resolves a server's public name the way the
internet does, so a LAN resolver that blocks the domain cannot make a working
edge read as a broken one. These pin the resolver order, what ends it, and that
the connection presents the real hostname to the address it resolved.

Run: uv run pytest tests/unit/test_public_https.py
"""
from __future__ import annotations

import json
import socket
import struct
import sys
from pathlib import Path
from unittest import mock

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import public_https as ph  # noqa: E402


def _qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def _response(query_id: int, name: str, *, rcode: int = 0, cname: str = "",
              addresses: tuple[str, ...] = ()) -> bytes:
    """A resolver's answer: the question, an optional CNAME, then A records,
    each owner name a compression pointer back to the question (offset 12)."""
    answers = []
    if cname:
        answers.append(b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, len(_qname(cname)))
                       + _qname(cname))
    for a in addresses:
        answers.append(b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4)
                       + socket.inet_aton(a))
    header = struct.pack(">HHHHHH", query_id, 0x8180 | rcode, 1, len(answers), 0, 0)
    return header + _qname(name) + struct.pack(">HH", 1, 1) + b"".join(answers)


def test_a_records_are_read_past_a_cname_and_compressed_names():
    data = _response(7, "cf-chain-probe-1.example.test", cname="x.example.test",
                     addresses=("104.16.1.1", "172.64.80.1"))
    assert ph.parse_response(data, 7, "n", "1.1.1.1") == ["104.16.1.1", "172.64.80.1"]


def test_an_answer_to_another_query_is_not_used():
    data = _response(7, "a.example.test", addresses=("104.16.1.1",))
    assert ph.parse_response(data, 8, "n", "1.1.1.1") is None


def test_nxdomain_reads_as_the_local_resolvers_no_such_name():
    data = _response(7, "a.example.test", rcode=3)
    with pytest.raises(socket.gaierror) as e:
        ph.parse_response(data, 7, "a.example.test", "1.1.1.1")
    assert "Name or service not known" in str(e.value)


def test_a_truncated_answer_is_not_used():
    data = _response(7, "a.example.test", addresses=("104.16.1.1",))
    assert ph.parse_response(data[:-3], 7, "n", "1.1.1.1") is None


def test_the_first_public_resolver_that_answers_decides(monkeypatch):
    asked = []

    def query(name, server, timeout=ph.DNS_TIMEOUT_S):
        asked.append(server)
        return None if server == "1.1.1.1" else ["104.16.1.1"]

    monkeypatch.setattr(ph, "query", query)
    monkeypatch.setattr(ph.socket, "getaddrinfo",
                        mock.Mock(side_effect=AssertionError("local resolver asked")))
    assert ph.resolve("a.example.test") == (["104.16.1.1"], "9.9.9.9")
    assert asked == ["1.1.1.1", "9.9.9.9"]


def test_a_public_no_such_name_is_final(monkeypatch):
    """A local resolver asked after a public NXDOMAIN gives the LAN's opinion
    of the name: the answer this module replaces."""
    def query(name, server, timeout=ph.DNS_TIMEOUT_S):
        raise ph._not_found(name, server)

    monkeypatch.setattr(ph, "query", query)
    monkeypatch.setattr(ph.socket, "getaddrinfo",
                        mock.Mock(side_effect=AssertionError("local resolver asked")))
    with pytest.raises(socket.gaierror):
        ph.resolve("a.example.test")


def test_the_local_resolver_decides_only_when_no_public_one_answers(monkeypatch):
    monkeypatch.setattr(ph, "query", lambda name, server, timeout=0: None)
    monkeypatch.setattr(ph.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.0", 443))])
    assert ph.resolve("a.example.test") == (["0.0.0.0"], "system")


def test_the_connection_presents_the_hostname_to_the_resolved_address(monkeypatch):
    created = mock.Mock(return_value=mock.sentinel.sock)
    monkeypatch.setattr(ph.socket, "create_connection", created)
    context = mock.Mock()
    conn = ph._PinnedHTTPSConnection("auth.example.test", "104.16.1.1",
                                     context=context, timeout=5)
    conn.connect()
    created.assert_called_once_with(("104.16.1.1", 443), 5)
    context.wrap_socket.assert_called_once_with(
        mock.sentinel.sock, server_hostname="auth.example.test")


def test_no_http_answer_is_status_minus_one_with_the_reason(monkeypatch, capsys):
    monkeypatch.setattr(ph, "fetch", mock.Mock(
        side_effect=ConnectionRefusedError(111, "Connection refused")))
    assert ph.main(["--url", "https://a.example.test/"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == -1
    assert "Connection refused" in out["error"]


def test_only_https_urls_are_fetched():
    with pytest.raises(ValueError):
        ph.fetch("http://a.example.test/")
