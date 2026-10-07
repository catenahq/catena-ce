"""checks.py: what only another machine can check, and the record the panel's
port-exposure card reads.

Run: uv run pytest installer/tests/test_the_outside_checks.py
"""
from __future__ import annotations

import json
import socket

import pytest

from catena_installer import checks


class FakeSession:
    user = "ops"

    def __init__(self, summary: dict | None = None, containers: str = ""):
        self.summary, self.containers = summary, containers
        self.commands: list[str] = []

    def output(self, command: str):
        self.commands.append(command)
        if "public-ports.summary.json" in command:
            return (0, json.dumps(self.summary)) if self.summary else (1, "")
        if "docker network inspect" in command:
            return 0, self.containers
        return 0, ""


@pytest.mark.parametrize("address", ["10.0.0.7", "192.168.1.5", "172.20.0.2",
                                     "100.64.0.5", "127.0.0.1", "169.254.1.1", "nonsense"])
def test_a_scan_of_a_private_address_says_nothing_about_the_internet(address):
    assert checks._private(address)


def test_a_public_address_is_scanned():
    assert not checks._private("8.8.8.8")
    assert not checks._private("2001:4860:4860::8888")


def test_the_scan_finds_what_answers():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert checks.scan("127.0.0.1", range(port, port + 1)) == [port]
    finally:
        listener.close()
    assert checks.scan("127.0.0.1", range(port, port + 1)) == []


def test_a_private_public_ip_skips_the_scan_out_loud():
    lines: list[str] = []
    session = FakeSession()
    assert checks.public_ports(session, "10.0.0.7", lines.append)
    assert "SKIPPED" in lines[0] and session.commands == []


def test_the_scan_is_recorded_on_the_server_and_judged(monkeypatch):
    monkeypatch.setattr(checks, "scan", lambda host: [22, 25, 5349])
    session = FakeSession({"public_open_tcp": [22, 5349]})
    lines: list[str] = []
    # A routable address: the documentation ranges count as private.
    assert not checks.public_ports(session, "8.8.4.4", lines.append)
    assert "SECURITY REGRESSION" in lines[-1] and "[25]" in lines[-1]
    write = next(c for c in session.commands if checks.RECORD in c)
    assert "sudo -n tee /var/lib/catena/port-scan.json" in write
    body = json.loads(write.split("printf '%s\\n' ", 1)[1].split(" | ", 1)[0].strip("'"))
    assert (body["exposed_count"], body["expected_count"], body["unexpected"]) == (1, 2, [25])
    assert isinstance(body["scanned"], int)

    monkeypatch.setattr(checks, "scan", lambda host: [22, 5349])
    assert checks.public_ports(FakeSession({"public_open_tcp": [22, 5349]}),
                               "8.8.4.4", lines.append)


def test_a_container_reachable_from_here_fails(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    connect = socket.create_connection
    monkeypatch.setattr(checks.socket, "create_connection",
                        lambda addr, timeout: connect(
                            ("127.0.0.1", listener.getsockname()[1]), timeout))
    lines: list[str] = []
    try:
        assert not checks.container_network(FakeSession(containers="10.0.1.5/24\n"),
                                            lines.append)
    finally:
        listener.close()
    assert "10.0.1.5" in lines[-1]


def test_containers_out_of_reach_pass(monkeypatch):
    def refuse(addr, timeout):
        raise ConnectionRefusedError
    monkeypatch.setattr(checks.socket, "create_connection", refuse)
    lines: list[str] = []
    assert checks.container_network(FakeSession(containers="10.0.1.5/24\n10.0.1.6/24\n"),
                                    lines.append)
    assert "2 container IP(s)" in lines[-1]
