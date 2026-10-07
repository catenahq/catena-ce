"""SSH to the server, the way the installer needs it: the host key checked
against ~/.ssh/known_hosts, a login with the key or once with the provider's
password, a command's output streamed back line by line, files sent for a
leg, and the panel account's forward to the two UIs on the server's loopback.

paramiko, which is pure Python, so the installer needs no SSH client and runs
alike on Windows, macOS and Linux. The host key policy is OpenSSH's: the first
key a server presents is trusted and recorded, and another key later is
refused unless the person running the install says the server was
reinstalled -- from here a reinstalled server and another machine answering at
the same address look alike.
"""

from __future__ import annotations

import re
import secrets
import select
import shlex
import shutil
import socket
import string
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import paramiko

KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"
TIMEOUT = 15.0

# The host key algorithms a known_hosts key type is offered under.
_ALGORITHMS = {"ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")}


class RemoteError(Exception):
    """The server could not be reached or used as the install needs."""


class AuthRefused(RemoteError):
    """The server answered and refused the login."""


class HostKeyChanged(RemoteError):
    """The server presents another host key than the one trusted for it."""

    def __init__(self, name: str, trusted: list[str], offered: str):
        super().__init__(
            f"{name} presents another host key than the one this machine "
            f"trusts for it (trusted: {', '.join(trusted)}; offered: {offered}). "
            "A reinstalled server presents a new key; if this one was not "
            "reinstalled, another machine may be answering at this address.")
        self.name, self.trusted, self.offered = name, trusted, offered


def known_name(host: str, port: int = 22) -> str:
    """The name OpenSSH files `host` under in known_hosts."""
    return host if port == 22 else f"[{host}]:{port}"


def _describe(key: paramiko.PKey) -> str:
    return f"{key.get_name()} {key.fingerprint}"


def _trusted(known_hosts: Path, name: str) -> list[paramiko.PKey]:
    if not known_hosts.is_file():
        return []
    entry = paramiko.HostKeys(str(known_hosts)).lookup(name)
    return list(entry.values()) if entry else []


def _record(known_hosts: Path, name: str, key: paramiko.PKey) -> None:
    """Append, never rewrite: the file is the person's own, with lines this
    installer does not read."""
    known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new = not known_hosts.exists()
    with known_hosts.open("a", encoding="utf-8") as fh:
        fh.write(f"{name} {key.get_name()} {key.get_base64()}\n")
    if new:
        known_hosts.chmod(0o600)


def forget(known_hosts: Path, name: str) -> None:
    """Drop every key trusted for `name`, hashed entries included."""
    if not known_hosts.is_file():
        return
    if shutil.which("ssh-keygen") is None:
        raise RemoteError("ssh-keygen is not installed: it ships with OpenSSH")
    subprocess.run(["ssh-keygen", "-R", name, "-f", str(known_hosts)],
                   capture_output=True, text=True, check=True)


def changed(host: str, port: int = 22,
            known_hosts: Path = KNOWN_HOSTS) -> tuple[list[str], str]:
    """The keys this machine trusts for the server and the one it offers,
    when it offers another; ([], "") when it offers a trusted one, when
    nothing is trusted for it yet, and when nothing answers. Records
    nothing."""
    try:
        _transport(host, port, known_hosts=known_hosts, reinstalled=False,
                   record=False).close()
    except HostKeyChanged as exc:
        return exc.trusted, exc.offered
    except RemoteError:
        pass
    return [], ""


def _transport(host: str, port: int, *, known_hosts: Path, reinstalled: bool,
               record: bool = True) -> paramiko.Transport:
    """A connected transport whose host key is the trusted one, or the first
    one this machine has seen for the server, which is recorded unless
    `record` is off (a probe leaves nothing behind)."""
    name = known_name(host, port)
    trusted = _trusted(known_hosts, name)
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
    except OSError as exc:
        raise RemoteError(f"nothing answers SSH at {name}: {exc}") from exc
    transport = paramiko.Transport(sock)
    if trusted and not reinstalled:
        # Ask for a key type this machine holds, as OpenSSH does: a server
        # also offering another type is not a changed server.
        options = transport.get_security_options()
        wanted = [a for k in trusted for a in _ALGORITHMS.get(k.get_name(), (k.get_name(),))]
        usable = tuple(dict.fromkeys(a for a in wanted if a in options.key_types))
        if usable:
            options.key_types = usable
    try:
        transport.start_client(timeout=TIMEOUT)
    except paramiko.SSHException as exc:
        transport.close()
        if trusted and not reinstalled:
            raise HostKeyChanged(name, [_describe(k) for k in trusted],
                                 "none of the trusted key types") from exc
        raise RemoteError(f"{name}: SSH negotiation failed: {exc}") from exc
    key = transport.get_remote_server_key()
    if not trusted:
        if record:
            _record(known_hosts, name, key)
    elif not any(k.get_name() == key.get_name() and k.asbytes() == key.asbytes()
                 for k in trusted):
        if not reinstalled:
            transport.close()
            raise HostKeyChanged(name, [_describe(k) for k in trusted], _describe(key))
        if record:
            forget(known_hosts, name)
            _record(known_hosts, name, key)
    return transport


def load_key(path: Path) -> paramiko.PKey:
    try:
        return paramiko.PKey.from_path(str(path))
    except (paramiko.PasswordRequiredException, TypeError) as exc:
        # An encrypted OpenSSH key reaches paramiko's loader as a TypeError
        # from cryptography, the older formats as PasswordRequiredException.
        raise RemoteError(f"{path} is protected by a passphrase; the installer "
                          "logs in unattended and needs a key without one") from exc
    except (OSError, paramiko.SSHException, ValueError) as exc:
        raise RemoteError(f"cannot read the private key {path}: {exc}") from exc


class Session:
    """One authenticated SSH connection."""

    def __init__(self, transport: paramiko.Transport, user: str, host: str):
        self.transport, self.user, self.host = transport, user, host

    def run(self, command: str, on_line: Callable[[str], None]) -> int:
        """Run `command` through the login shell, passing each line it prints
        (stdout and stderr together) to `on_line`; return its exit status."""
        chan = self.transport.open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(command)
        pending = b""
        while True:
            data = chan.recv(65536)
            if not data:
                break
            pending += data
            *lines, pending = pending.split(b"\n")
            for line in lines:
                on_line(line.decode("utf-8", "replace").rstrip("\r"))
        if pending:
            on_line(pending.decode("utf-8", "replace").rstrip("\r"))
        return chan.recv_exit_status()

    def output(self, command: str) -> tuple[int, str]:
        lines: list[str] = []
        rc = self.run(command, lines.append)
        return rc, "\n".join(lines)

    def upload(self, files: dict[str, bytes]) -> str:
        """Write `files` into a new 0700 directory under /tmp, each 0600, and
        return the directory. Through the shell rather than SFTP, so a server
        whose sshd serves no SFTP subsystem takes them too."""
        rc, out = self.output("mktemp -d /tmp/catena-install.XXXXXX")
        if rc != 0:
            raise RemoteError(f"cannot create an upload directory: {out}")
        directory = out.strip().splitlines()[-1]
        for name, data in files.items():
            chan = self.transport.open_session()
            chan.exec_command(f"umask 077 && cat > {shlex.quote(directory + '/' + name)}")
            chan.sendall(data)
            chan.shutdown_write()
            if chan.recv_exit_status() != 0:
                raise RemoteError(f"cannot write {name} into {directory}")
        return directory

    def close(self) -> None:
        self.transport.close()


def connect_key(host: str, port: int, user: str, key: Path, *,
                known_hosts: Path = KNOWN_HOSTS, reinstalled: bool = False,
                record: bool = True) -> Session:
    transport = _transport(host, port, known_hosts=known_hosts,
                           reinstalled=reinstalled, record=record)
    try:
        transport.auth_publickey(user, load_key(key))
    except paramiko.AuthenticationException as exc:
        transport.close()
        raise AuthRefused(f"{user}@{host} refused the key {key}") from exc
    except (paramiko.SSHException, RemoteError):
        transport.close()
        raise
    return Session(transport, user, host)


# --- the provider's password -------------------------------------------------
# Used once, to put the installer's key on the provider's login. Some providers
# issue a password that must be changed at first login (OVH), and sshd asks
# for that change in the session's terminal, so the session is a shell on a
# pseudo-terminal driven by what it prints.

_CHANGE_WARNINGS = [r"required to change your password", r"[Pp]assword has expired",
                    r"must change your password", r"Changing password for"]
_CURRENT_PW = [r"[Cc]urrent.*password:", r"UNIX password:"]
_NEW_PW = [r"[Nn]ew password:", r"[Nn]ew UNIX password:"]
_RETYPE_PW = [r"[Rr]etype new password:", r"[Rr]e-?enter new password:"]


class _Terminal:
    """A shell on a pseudo-terminal, read until one of several patterns."""

    def __init__(self, chan: paramiko.Channel):
        self.chan, self.buffer = chan, ""

    def expect(self, patterns: list[str], timeout: float) -> int:
        """The index of the pattern found earliest in what the shell printed
        next; TimeoutError when none appears in time."""
        compiled = [re.compile(p) for p in patterns]
        deadline = time.monotonic() + timeout
        while True:
            found = [(m.start(), i, m.end()) for i, p in enumerate(compiled)
                     if (m := p.search(self.buffer))]
            if found:
                _, index, end = min(found)
                self.buffer = self.buffer[end:]
                return index
            if time.monotonic() > deadline:
                raise TimeoutError(f"waited {timeout:.0f}s for {patterns}")
            if self.chan.recv_ready():
                self.buffer += self.chan.recv(4096).decode("utf-8", "replace")
            elif self.chan.closed or self.chan.exit_status_ready():
                raise EOFError("the shell closed")
            else:
                time.sleep(0.05)

    def send(self, line: str) -> None:
        self.chan.send(line + "\n")

    def sync(self, timeout: float = 60) -> None:
        """Wait until the shell runs what it is given, whatever banner and
        prompt came before: echo a marker and wait for it."""
        marker = "__CATENA_" + secrets.token_hex(6) + "__"
        self.send("")
        self.send(f"echo {marker}")
        self.expect([re.escape(marker) + r"\r?\n"], timeout)


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^&*_+-="
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _password_shell(host: str, port: int, user: str, password: str, *,
                    known_hosts: Path, reinstalled: bool,
                    record: bool = True) -> tuple[paramiko.Transport, _Terminal]:
    transport = _transport(host, port, known_hosts=known_hosts,
                           reinstalled=reinstalled, record=record)
    try:
        transport.auth_password(user, password)
    except paramiko.AuthenticationException as exc:
        transport.close()
        raise AuthRefused(f"{user}@{host} refused the password") from exc
    chan = transport.open_session()
    chan.get_pty()
    chan.invoke_shell()
    return transport, _Terminal(chan)


def _change_prompt(term: _Terminal) -> str:
    """"current", "new" or "" (no change asked) for what follows a login."""
    try:
        i = term.expect(_CHANGE_WARNINGS + _CURRENT_PW + _NEW_PW, timeout=10)
    except TimeoutError:
        return ""
    if i < len(_CHANGE_WARNINGS):
        i = len(_CHANGE_WARNINGS) + term.expect(_CURRENT_PW + _NEW_PW, timeout=30)
    return "current" if i < len(_CHANGE_WARNINGS) + len(_CURRENT_PW) else "new"


def check_password(host: str, port: int, user: str, password: str, *,
                   known_hosts: Path = KNOWN_HOSTS, reinstalled: bool = False) -> tuple[bool, str]:
    """Whether `password` opens `user`. Changes nothing: a server that asks
    for a new password at first login is left asking, and counts as opened,
    because the install answers that prompt itself."""
    try:
        transport, term = _password_shell(host, port, user, password,
                                          known_hosts=known_hosts,
                                          reinstalled=reinstalled, record=False)
    except AuthRefused:
        return False, f"{user}@{host} refused the password"
    try:
        if _change_prompt(term):
            return True, (f"the password opens {user}@{host}; the server asks for "
                          "a new one at first login, which the install sets")
        term.sync(timeout=30)
        return True, f"the password opens {user}@{host}"
    except (TimeoutError, EOFError) as exc:
        return False, f"no answer from {user}@{host}: {exc}"
    finally:
        transport.close()


def install_key_with_password(host: str, port: int, user: str, password: str,
                              public_line: str, *, known_hosts: Path = KNOWN_HOSTS,
                              reinstalled: bool = False) -> None:
    """Add `public_line` to `user`'s authorized_keys, logging in with the
    provider's password and setting a new random one first when the server
    demands it (that password is never used again: the hardening that follows
    turns password logins off)."""
    for _ in range(2):
        transport, term = _password_shell(host, port, user, password,
                                          known_hosts=known_hosts, reinstalled=reinstalled)
        try:
            change = _change_prompt(term)
            if change:
                if change == "current":
                    term.send(password)
                    term.expect(_NEW_PW, timeout=15)
                password = _random_password()
                term.send(password)
                term.expect(_RETYPE_PW, timeout=15)
                term.send(password)
                # sshd usually ends the session once the password changes; the
                # loop logs in again with the new one.
                try:
                    term.sync(timeout=10)
                except (TimeoutError, EOFError):
                    continue
            else:
                term.sync()
            _append_key(term, user, public_line)
            return
        except (TimeoutError, EOFError) as exc:
            raise RemoteError(f"the session as {user}@{host} stopped answering: {exc}") from exc
        finally:
            transport.close()
    raise RemoteError(f"{user}@{host} kept asking for a new password")


def _append_key(term: _Terminal, user: str, public_line: str) -> None:
    """Each command stays well under the pseudo-terminal's 255-byte line
    limit, past which the line is cut and the shell drops it unseen: the key is
    staged in a file and every command refers to it by path."""
    keyfile = f"/tmp/.catena-key-{secrets.token_hex(4)}"
    home = "/root" if user == "root" else "~"
    commands = [
        f"echo {shlex.quote(public_line)} > {keyfile}",
        f"install -d -m 700 {home}/.ssh",
        f"grep -qxF \"$(cat {keyfile})\" {home}/.ssh/authorized_keys 2>/dev/null"
        f" || cat {keyfile} >> {home}/.ssh/authorized_keys",
        f"chmod 600 {home}/.ssh/authorized_keys",
        f"rm -f {keyfile}",
    ]
    for command in commands:
        marker = "__CATENA_" + secrets.token_hex(6) + "__"
        term.send(f"{command}; echo {marker}")
        term.expect([re.escape(marker) + r"\r?\n"], timeout=30)
    term.send("exit")


# --- the panel forward -------------------------------------------------------

class Forward:
    """Local ports on 127.0.0.1 forwarded through `session` to ports on the
    server's loopback, as `ssh -L` does. Each local port is the remote one
    when it is free here, else one the system picks."""

    def __init__(self, session: Session, remote_ports: list[int]):
        self.session = session
        self.local: dict[int, int] = {}
        self._listeners: list[socket.socket] = []
        self._closed = threading.Event()
        for remote in remote_ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", remote))
            except OSError:
                listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            listener.settimeout(1.0)
            self.local[remote] = listener.getsockname()[1]
            self._listeners.append(listener)
            threading.Thread(target=self._accept, args=(listener, remote),
                             daemon=True).start()

    def _accept(self, listener: socket.socket, remote: int) -> None:
        while not self._closed.is_set():
            try:
                conn, origin = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                chan = self.session.transport.open_channel(
                    "direct-tcpip", ("127.0.0.1", remote), origin)
            except paramiko.SSHException:
                conn.close()
                continue
            threading.Thread(target=_pipe, args=(conn, chan), daemon=True).start()

    def url(self, remote: int) -> str:
        return f"http://127.0.0.1:{self.local[remote]}/"

    def close(self) -> None:
        self._closed.set()
        for listener in self._listeners:
            listener.close()
        self.session.close()


def _pipe(conn: socket.socket, chan: paramiko.Channel) -> None:
    try:
        while True:
            ready, _, _ = select.select([conn, chan], [], [], 1.0)
            if conn in ready:
                data = conn.recv(65536)
                if not data:
                    break
                chan.sendall(data)
            if chan in ready:
                data = chan.recv(65536)
                if not data:
                    break
                conn.sendall(data)
    except OSError:
        pass
    finally:
        chan.close()
        conn.close()
