"""remote.py against a real SSH server in this process (paramiko's own server
side): the host key trusted on first contact and refused once it changes, a
login with the key or not at all, a command's output streamed back, files
sent, and the panel forward.

Run: uv run pytest installer/tests/test_remote.py
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import paramiko
import pytest

from catena_installer import remote

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None,
                                reason="needs ssh-keygen")


class _Server(paramiko.ServerInterface):
    """Accepts `client_key` for ops; answers the commands the installer
    sends; opens direct-tcpip channels."""

    def __init__(self, client_key: paramiko.PKey, files: dict):
        self.client_key, self.files = client_key, files
        self.destinations: dict[int, tuple[str, int]] = {}

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        if username == "ops" and key.asbytes() == self.client_key.asbytes():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.destinations[chanid] = destination
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self._exec, args=(channel, command.decode()),
                         daemon=True).start()
        return True

    def _exec(self, channel, command):
        # After paramiko has answered the exec request, which it does once
        # check_channel_exec_request returns.
        time.sleep(0.1)
        if command.startswith("mktemp"):
            channel.sendall(b"/tmp/catena-install.test\n")
            status = 0
        elif command.startswith("umask 077 && cat > "):
            path = command.split("cat > ", 1)[1].strip("'")
            data = b""
            while chunk := channel.recv(4096):
                data += chunk
            self.files[path] = data
            status = 0
        else:
            channel.sendall(b"first line\nsecond line\nno newline")
            status = 3
        channel.send_exit_status(status)
        channel.close()


def _serve(host_key, client_key, files) -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def connection(conn):
        iface = _Server(client_key, files)
        transport = paramiko.Transport(conn)
        transport.add_server_key(host_key)
        try:
            transport.start_server(server=iface)
        except (paramiko.SSHException, OSError, EOFError):
            return  # a probe that hung up after reading the host key
        # Held, or a session channel closes when it is collected.
        sessions = []
        while transport.is_active():
            chan = transport.accept(1)
            if chan is None:
                continue
            if chan.get_id() not in iface.destinations:
                sessions.append(chan)
                continue
            target = socket.create_connection(("127.0.0.1", iface.destinations[chan.get_id()][1]))
            threading.Thread(target=remote._pipe, args=(target, chan), daemon=True).start()

    def accept():
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=connection, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener.getsockname()[1]


def _keygen(path: Path, passphrase: str = "") -> Path:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)],
                   check=True)
    return path


@pytest.fixture
def client_key(tmp_path) -> Path:
    return _keygen(tmp_path / "id_ed25519")


@pytest.fixture
def server(tmp_path, client_key):
    host_key = paramiko.RSAKey.generate(2048)
    files: dict = {}
    port = _serve(host_key, remote.load_key(client_key), files)
    return {"port": port, "files": files, "host_key": host_key,
            "known": tmp_path / "known_hosts"}


def _connect(server, key, **kw):
    return remote.connect_key("127.0.0.1", server["port"], "ops", key,
                              known_hosts=server["known"], **kw)


def test_the_first_key_is_trusted_and_recorded_once(server, client_key):
    _connect(server, client_key).close()
    _connect(server, client_key).close()
    lines = server["known"].read_text().splitlines()
    name = remote.known_name("127.0.0.1", server["port"])
    assert lines == [f"{name} ssh-rsa {server['host_key'].get_base64()}"]


def test_a_command_streams_its_lines_and_its_status(server, client_key):
    session = _connect(server, client_key)
    seen: list[str] = []
    assert session.run("anything", seen.append) == 3
    assert seen == ["first line", "second line", "no newline"]
    session.close()


def test_files_go_up_through_the_shell(server, client_key):
    session = _connect(server, client_key)
    directory = session.upload({"env": b"HOST_PUBLIC_IP=203.0.113.10\n"})
    session.close()
    assert directory == "/tmp/catena-install.test"
    assert server["files"] == {
        "/tmp/catena-install.test/env": b"HOST_PUBLIC_IP=203.0.113.10\n"}


def test_a_key_the_server_does_not_accept_is_refused(server, tmp_path):
    with pytest.raises(remote.AuthRefused):
        _connect(server, _keygen(tmp_path / "other"))


def _trust_another_key(server) -> str:
    name = remote.known_name("127.0.0.1", server["port"])
    other = paramiko.RSAKey.generate(2048)
    server["known"].write_text(f"{name} ssh-rsa {other.get_base64()}\n")
    return other.get_base64()


def test_a_changed_host_key_is_refused_and_nothing_is_written(server, client_key):
    _trust_another_key(server)
    before = server["known"].read_text()
    with pytest.raises(remote.HostKeyChanged) as raised:
        _connect(server, client_key)
    assert server["known"].read_text() == before
    assert raised.value.offered.startswith("ssh-rsa SHA256:")
    assert raised.value.offered not in raised.value.trusted


def test_a_reinstalled_server_replaces_the_trusted_key(server, client_key):
    _trust_another_key(server)
    _connect(server, client_key, reinstalled=True).close()
    lines = server["known"].read_text().splitlines()
    assert len(lines) == 1 and server["host_key"].get_base64() in lines[0]
    _connect(server, client_key).close()


def test_the_probe_names_both_keys_and_records_nothing(server):
    assert remote.changed("127.0.0.1", server["port"], server["known"]) == ([], "")
    assert not server["known"].exists(), "a probe recorded the first key it saw"
    _trust_another_key(server)
    trusted, offered = remote.changed("127.0.0.1", server["port"], server["known"])
    assert len(trusted) == 1 and offered.startswith("ssh-rsa SHA256:")


def test_nothing_listening_is_a_remote_error(tmp_path, client_key):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(remote.RemoteError):
        remote.connect_key("127.0.0.1", port, "ops", client_key,
                           known_hosts=tmp_path / "known_hosts")


def test_a_key_with_a_passphrase_is_named_as_such(tmp_path):
    with pytest.raises(remote.RemoteError, match="passphrase"):
        remote.load_key(_keygen(tmp_path / "locked", "secret"))


def test_the_forward_reaches_the_servers_loopback(server, client_key):
    """A connection to the local port lands on the server's loopback port,
    as `ssh -L` does; a local port already taken gets another one."""
    panel = socket.socket()
    panel.bind(("127.0.0.1", 0))
    panel.listen(1)
    panel_port = panel.getsockname()[1]

    def answer():
        conn, _ = panel.accept()
        conn.sendall(b"panel:" + conn.recv(64))
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    forward = remote.Forward(_connect(server, client_key), [panel_port])
    try:
        assert forward.local[panel_port] != panel_port
        with socket.create_connection(("127.0.0.1", forward.local[panel_port])) as conn:
            conn.sendall(b"hello")
            assert conn.recv(64) == b"panel:hello"
        assert forward.url(panel_port) == f"http://127.0.0.1:{forward.local[panel_port]}/"
    finally:
        forward.close()
