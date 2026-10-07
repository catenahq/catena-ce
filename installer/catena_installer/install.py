"""The install, as the page and the console run it.

  1. log in: as ops with the inventory's key on a server installed before,
     else as the provider's login with the key, else once with the provider's
     password to add the key to that login;
  2. leg `accounts`, through that login: the starter script installs what the
     release needs to start, fetches the release image's tree and payload
     (fetch_release.py), and the tree's install-host.sh creates ops and the
     panel account with this machine's public key. Nothing is closed;
  3. log in again, as ops with the key: that login is the proof the hardening
     waits for;
  4. leg `system`: install-host.sh proves this session from sshd's own record,
     then runs bootstrap, prints the passwords between the KEYSET markers,
     converges, records the version and runs the server's own checks;
  5. the checks only another machine can make (checks.py).

The server's output comes back through the SSH session line by line. The
inventory's `.env`, this machine's public key and, on leg `system`, the
transient adopt file (an admin password the install pins) go up with each leg,
into a 0700 directory removed when the leg ends.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from . import checks, keys, remote, tree

REPOSITORY = "ghcr.io/catenahq/catena-admin"
KEYSET_BEGIN = "-----BEGIN CATENA PASSWORDS-----"
KEYSET_END = "-----END CATENA PASSWORDS-----"

# The host-only UIs the panel account forwards to. Product constants
# (catena-ce playbooks/group_vars/all/main.yml).
PANEL_PORT = 9010
PORTAINER_PORT = 9000
PANEL_USER = "panel"

# install-host.sh's status for an install whose checks failed.
VALIDATION_FAILED = 3


class PasswordNeeded(remote.AuthRefused):
    """No key opens the server yet and no provider password was given."""


@dataclass
class Server:
    """What an inventory says about its server."""

    inventory: Path
    name: str
    host: str
    port: int
    initial_user: str
    ops_user: str
    key: Path
    public_ip: str

    @classmethod
    def of(cls, inv_dir: Path, address: str = "") -> "Server":
        """The address is `address`, else a literal one hosts.yml gives its
        vps host, else HOST_SSH_ADDRESS, else HOST_PUBLIC_IP."""
        env = tree.seed().read_existing_env(inv_dir / ".env")
        name, listed = _vps_host(inv_dir)
        port = (env.get("HOST_SSH_PORT") or "22").strip() or "22"
        return cls(
            inventory=inv_dir,
            name=name or f"{inv_dir.name}1",
            host=(address or listed or (env.get("HOST_SSH_ADDRESS") or "").strip()
                  or (env.get("HOST_PUBLIC_IP") or "").strip()),
            port=int(port),
            initial_user=(env.get("HOST_INITIAL_USER") or "root").strip() or "root",
            ops_user=(env.get("OPS_USER") or "ops").strip() or "ops",
            key=Path(env.get("SSH_PRIVATE_KEY") or keys.DEFAULT_KEY).expanduser(),
            public_ip=(env.get("HOST_PUBLIC_IP") or "").strip(),
        )


def _vps_host(inv_dir: Path) -> tuple[str, str]:
    """The first host of hosts.yml's vps group, and its address when the file
    names a literal one."""
    hosts_yml = inv_dir / "hosts.yml"
    if not hosts_yml.is_file():
        return "", ""
    doc = yaml.safe_load(hosts_yml.read_text(encoding="utf-8")) or {}
    hosts = ((((doc.get("all") or {}).get("children") or {}).get("vps") or {})
             .get("hosts") or {})
    for name, entry in hosts.items():
        address = str((entry or {}).get("ansible_host") or "")
        if address == "0.0.0.0" or "{{" in address:
            address = ""
        return str(name), address
    return "", ""


@dataclass
class Options:
    release: str = ""
    image: str = ""
    ca_file: str = ""
    insecure_http: bool = False
    address: str = ""
    tags: str = ""
    reinstalled: bool = False
    keyset_json: bool = False
    password: str = ""
    adopt: dict[str, str] = field(default_factory=dict)
    outside_checks: bool = True


class Output:
    """Where the install's lines go: every line to `line`, except the
    passwords block, whose lines go to `keyset` in one call."""

    def __init__(self, line: Callable[[str], None],
                 keyset: Callable[[list[str]], None]):
        self.line, self.keyset = line, keyset
        self._block: list[str] | None = None

    def feed(self, text: str) -> None:
        if text == KEYSET_BEGIN:
            self._block = []
        elif self._block is None:
            self.line(text)
        elif text == KEYSET_END:
            self.keyset(self._block)
            self._block = None
        else:
            self._block.append(text)


def login(server: Server, opts: Options, line: Callable[[str], None]) -> remote.Session:
    """The first login of an install."""
    refused = []
    for user in dict.fromkeys((server.ops_user, server.initial_user)):
        try:
            return remote.connect_key(server.host, server.port, user, server.key,
                                      reinstalled=opts.reinstalled)
        except remote.AuthRefused as exc:
            refused.append(str(exc))
    if not opts.password:
        raise PasswordNeeded(
            f"{'; '.join(refused)}, and no provider password was given to add "
            f"the key to {server.initial_user}@{server.host} with")
    line(f"catena-installer: adding {server.key}.pub to "
         f"{server.initial_user}@{server.host} with the provider's password")
    remote.install_key_with_password(
        server.host, server.port, server.initial_user, opts.password,
        keys.public_line(server.key), reinstalled=opts.reinstalled)
    return _connect_retrying(server, server.initial_user)


def _connect_retrying(server: Server, user: str) -> remote.Session:
    """A login right after the account or key was made: a fresh server's sshd
    can read authorized_keys a moment after the command that wrote it ended."""
    for attempt in range(1, 6):
        try:
            return remote.connect_key(server.host, server.port, user, server.key)
        except remote.AuthRefused:
            if attempt == 5:
                raise
            time.sleep(3)
    raise AssertionError("unreachable")


def _leg(session: remote.Session, server: Server, opts: Options,
         feed: Callable[[str], None], leg: str) -> int:
    """Send the leg's files, run the starter through sudo, stream its output
    to `feed`, and remove the files."""
    fetch_release = leg in ("accounts", "uninstall")
    files = {
        "starter.sh": tree.STARTER.read_bytes(),
        "env": (server.inventory / ".env").read_bytes(),
        "ops.pub": (keys.public_line(server.key) + "\n").encode(),
    }
    if fetch_release:
        for rel in tree.FETCHER:
            files[Path(rel).name] = (tree.TREE / rel).read_bytes()
        if opts.ca_file:
            files["ca.crt"] = Path(opts.ca_file).expanduser().read_bytes()
    if leg == "system" and opts.adopt:
        files["adopt.yml"] = yaml.safe_dump(opts.adopt).encode()
    upload = session.upload(files)

    def at(name: str) -> str:
        return f"{upload}/{name}"

    if fetch_release:
        fetch = ["--repository", REPOSITORY]
        if opts.image:
            fetch += ["--image", opts.image]
        elif opts.release:
            fetch += ["--release", opts.release]
        if opts.ca_file:
            fetch += ["--ca-file", at("ca.crt")]
        if opts.insecure_http:
            fetch.append("--insecure-http")
    else:
        fetch = ["--no-fetch"]
    host_args = ["--leg", leg, "--name", server.name, "--ops-user", server.ops_user,
                 "--ops-key", at("ops.pub"), "--env-file", at("env"),
                 "--address", server.host]
    if "adopt.yml" in files:
        host_args += ["--adopt-file", at("adopt.yml")]
    if leg == "system" and opts.tags:
        host_args += ["--tags", opts.tags]
    if leg == "system" and opts.keyset_json:
        host_args.append("--keyset-json")
    sudo = "" if session.user == "root" else "sudo -n "
    command = (f"{sudo}bash {shlex.quote(at('starter.sh'))} "
               f"{shlex.join(fetch)} -- {shlex.join(host_args)}")
    if leg == "system":
        # Expanded by the server's shell: the session install-host.sh proves.
        command += ' --session "$SSH_CONNECTION"'
    try:
        return session.run(command, feed)
    finally:
        session.run(f"rm -rf {shlex.quote(upload)}", lambda _line: None)


def run(inv_dir: Path, opts: Options, out: Output) -> int:
    """Install or re-apply the server of `inv_dir`; the exit status
    install-host.sh gave, or 1 when the installer could not get that far.

    A changed host key and a missing provider password raise
    (remote.HostKeyChanged, PasswordNeeded): only the person running the
    install can answer them, and the caller asks."""
    server = Server.of(inv_dir, opts.address)
    if not server.host:
        out.line("catena-installer: the inventory names no address for the "
                 "server (HOST_PUBLIC_IP or HOST_SSH_ADDRESS in its .env)")
        return 1
    try:
        session = login(server, opts, out.line)
        out.line(f"catena-installer: logged in as {session.user}@{server.host}")
        try:
            rc = _leg(session, server, opts, out.feed, "accounts")
        finally:
            session.close()
        if rc != 0:
            return rc
        out.line(f"catena-installer: logging in again, as {server.ops_user} "
                 f"with {server.key}")
        session = _connect_retrying(server, server.ops_user)
        try:
            rc = _leg(session, server, opts, out.feed, "system")
            if rc in (0, VALIDATION_FAILED) and opts.outside_checks:
                if not checks.run(session, server.public_ip, out.line):
                    rc = rc or VALIDATION_FAILED
        finally:
            session.close()
        return rc
    except (remote.HostKeyChanged, PasswordNeeded):
        raise
    except (remote.RemoteError, keys.KeyPairError, OSError) as exc:
        out.line(f"catena-installer: {exc}")
        return 1


def uninstall(inv_dir: Path, opts: Options, line: Callable[[str], None]) -> int:
    """Hand the server's OS updates back to Debian (playbooks/uninstall.yml),
    with the release it runs."""
    server = Server.of(inv_dir, opts.address)
    try:
        session = remote.connect_key(server.host, server.port, server.ops_user,
                                     server.key, reinstalled=opts.reinstalled)
        try:
            return _leg(session, server, opts, line, "uninstall")
        finally:
            session.close()
    except (remote.RemoteError, keys.KeyPairError, OSError) as exc:
        line(f"catena-installer: {exc}")
        return 1


def connect(inv_dir: Path, address: str = "") -> remote.Forward:
    """The panel and Portainer, forwarded to this machine through the panel
    account, which can do nothing but that."""
    server = Server.of(inv_dir, address)
    session = remote.connect_key(server.host, server.port, PANEL_USER, server.key)
    return remote.Forward(session, [PANEL_PORT, PORTAINER_PORT])
