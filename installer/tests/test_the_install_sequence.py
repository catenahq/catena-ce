"""The install's sequence: who it logs in as, what each leg sends and runs, and
what it hands back.

The lockout rule lives in the order: the first leg runs through the
provider's login and closes nothing, the second runs only through a fresh
login as ops with the key, and the server proves that login before it
hardens anything. These drive install.run against a fake server.

Run: uv run pytest installer/tests/test_the_install_sequence.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from catena_installer import checks, install, remote

ANSIBLE = Path(__file__).resolve().parents[2] / "ansible"
UPLOAD = "/tmp/catena-install.abc123"


class FakeSession:
    def __init__(self, user: str, server: "FakeServer"):
        self.user, self.server = user, server

    def upload(self, files: dict[str, bytes]) -> str:
        self.server.events.append(("upload", self.user, dict(files)))
        return UPLOAD

    def run(self, command: str, on_line) -> int:
        self.server.events.append(("run", self.user, command))
        if command.startswith("rm -rf"):
            return 0
        leg = re.search(r"--leg (\w+)", command).group(1)
        for line in self.server.prints.get(leg, []):
            on_line(line)
        rc = self.server.rc.get(leg, 0)
        if leg == "accounts" and rc == 0:
            self.server.key_opens.add("ops")
        return rc

    def output(self, command: str):
        return 0, ""

    def close(self) -> None:
        self.server.events.append(("close", self.user))


class FakeServer:
    def __init__(self, key_opens: set[str]):
        self.key_opens = set(key_opens)
        self.events: list[tuple] = []
        self.prints: dict[str, list[str]] = {}
        self.rc: dict[str, int] = {}
        self.passwords: list[tuple] = []

    def connect_key(self, host, port, user, key, *, reinstalled=False, record=True):
        self.events.append(("login", user))
        if user not in self.key_opens:
            raise remote.AuthRefused(f"{user}@{host} refused the key {key}")
        return FakeSession(user, self)

    def install_key_with_password(self, host, port, user, password, public_line,
                                  *, reinstalled=False):
        self.passwords.append((user, password, public_line))
        self.key_opens.add(user)

    def runs(self) -> list[tuple[str, str]]:
        return [(e[1], e[2]) for e in self.events
                if e[0] == "run" and not e[2].startswith("rm -rf")]

    def uploads(self) -> list[dict]:
        return [e[2] for e in self.events if e[0] == "upload"]


@pytest.fixture
def inventory(tmp_path):
    key = tmp_path / "catena_ed25519"
    key.write_text("private")
    (tmp_path / "catena_ed25519.pub").write_text("ssh-ed25519 AAAAkey catena\n")
    inv = tmp_path / "clientco"
    inv.mkdir()
    (inv / ".env").write_text(
        "HOST_PUBLIC_IP=203.0.113.10\nHOST_INITIAL_USER=debian\nOPS_USER=ops\n"
        f"SSH_PRIVATE_KEY={key}\n")
    (inv / "hosts.yml").write_text(
        "all:\n  children:\n    vps:\n      hosts:\n        clientco1:\n"
        "          ansible_host: 0.0.0.0\n")
    return inv


def _wire(monkeypatch, server: FakeServer, checks_pass: bool = True) -> list:
    monkeypatch.setattr(remote, "connect_key", server.connect_key)
    monkeypatch.setattr(remote, "install_key_with_password", server.install_key_with_password)
    monkeypatch.setattr(install.time, "sleep", lambda s: None)
    ran: list = []
    monkeypatch.setattr(checks, "run", lambda session, ip, line: ran.append(
        (session.user, ip)) or checks_pass)
    return ran


def _run(inventory, **opts) -> tuple[int, list[str], list[list[str]]]:
    lines, blocks = [], []
    rc = install.run(inventory, install.Options(**opts), install.Output(lines.append, blocks.append))
    return rc, lines, blocks


def test_a_new_server_installs_through_the_provider_login_then_ops(monkeypatch, inventory):
    server = FakeServer(key_opens={"debian"})
    _wire(monkeypatch, server)
    rc, _, _ = _run(inventory)
    assert rc == 0
    (first_user, accounts), (second_user, system) = server.runs()
    assert (first_user, second_user) == ("debian", "ops")
    assert accounts.startswith(f"sudo -n bash {UPLOAD}/starter.sh --repository "
                               f"{install.REPOSITORY} -- --leg accounts ")
    assert "--session" not in accounts
    assert system.startswith(f"sudo -n bash {UPLOAD}/starter.sh --no-fetch -- --leg system ")
    assert system.endswith(' --session "$SSH_CONNECTION"')
    for command in (accounts, system):
        for arg in ("--name clientco1", "--ops-user ops", f"--ops-key {UPLOAD}/ops.pub",
                    f"--env-file {UPLOAD}/env", "--address 203.0.113.10"):
            assert arg in command, (arg, command)
    logins = [e[1] for e in server.events if e[0] == "login"]
    assert logins == ["ops", "debian", "ops"], (
        "the second leg must run through a fresh login as ops")


def test_each_leg_sends_what_it_needs_and_removes_it(monkeypatch, inventory):
    server = FakeServer(key_opens={"ops"})
    _wire(monkeypatch, server)
    rc, _, _ = _run(inventory, adopt={"admin_password": "pinned-admin-password"})
    assert rc == 0
    accounts, system = server.uploads()
    assert set(accounts) == {"starter.sh", "env", "ops.pub", "fetch_release.py",
                             "catena_admin_release.py", "image_pin.py"}
    assert set(system) == {"starter.sh", "env", "ops.pub", "adopt.yml"}
    assert b"pinned-admin-password" in system["adopt.yml"]
    assert accounts["ops.pub"] == b"ssh-ed25519 AAAAkey catena\n"
    assert accounts["env"] == (inventory / ".env").read_bytes()
    removals = [e for e in server.events if e[0] == "run" and e[2].startswith("rm -rf")]
    assert len(removals) == 2 and all(UPLOAD in e[2] for e in removals)


def test_a_server_installed_before_is_reached_as_ops_first(monkeypatch, inventory):
    server = FakeServer(key_opens={"ops"})
    _wire(monkeypatch, server)
    assert _run(inventory)[0] == 0
    assert [e[1] for e in server.events if e[0] == "login"][0] == "ops"
    assert all(user == "ops" for user, _ in server.runs())


def test_root_runs_the_legs_without_sudo(monkeypatch, inventory):
    (inventory / ".env").write_text((inventory / ".env").read_text().replace(
        "HOST_INITIAL_USER=debian", "HOST_INITIAL_USER=root"))
    server = FakeServer(key_opens={"root"})
    _wire(monkeypatch, server)
    assert _run(inventory)[0] == 0
    assert server.runs()[0][1].startswith(f"bash {UPLOAD}/starter.sh")


def test_no_key_and_no_password_asks_the_caller(monkeypatch, inventory):
    server = FakeServer(key_opens=set())
    _wire(monkeypatch, server)
    with pytest.raises(install.PasswordNeeded):
        _run(inventory)
    assert server.runs() == []


def test_the_provider_password_adds_this_machines_key_once(monkeypatch, inventory):
    server = FakeServer(key_opens=set())
    _wire(monkeypatch, server)
    rc, lines, _ = _run(inventory, password="provider-pw")
    assert rc == 0
    assert server.passwords == [("debian", "provider-pw", "ssh-ed25519 AAAAkey catena")]
    assert not any("provider-pw" in line for line in lines)


def test_a_changed_host_key_reaches_the_caller(monkeypatch, inventory):
    def changed(*a, **k):
        raise remote.HostKeyChanged("203.0.113.10", ["ssh-ed25519 SHA256:old"],
                                    "ssh-ed25519 SHA256:new")
    monkeypatch.setattr(remote, "connect_key", changed)
    with pytest.raises(remote.HostKeyChanged):
        _run(inventory)


@pytest.mark.parametrize("opts, expected", [
    ({"release": "v1.2.3"}, ["--release", "v1.2.3"]),
    ({"image": "127.0.0.1:5000/catenahq/catena-admin:bench"},
     ["--image", "127.0.0.1:5000/catenahq/catena-admin:bench"]),
])
def test_the_release_or_the_image_reaches_the_fetch(monkeypatch, inventory, opts, expected):
    server = FakeServer(key_opens={"ops"})
    _wire(monkeypatch, server)
    _run(inventory, **opts)
    accounts = server.runs()[0][1]
    assert " ".join(expected) in accounts.split(" -- ")[0]


def test_a_registry_ca_goes_up_with_the_fetch(monkeypatch, inventory, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("CA")
    server = FakeServer(key_opens={"ops"})
    _wire(monkeypatch, server)
    _run(inventory, ca_file=str(ca), insecure_http=True)
    assert server.uploads()[0]["ca.crt"] == b"CA"
    fetch = server.runs()[0][1].split(" -- ")[0]
    assert f"--ca-file {UPLOAD}/ca.crt" in fetch and "--insecure-http" in fetch


def test_tags_and_json_passwords_ride_the_system_leg(monkeypatch, inventory):
    server = FakeServer(key_opens={"ops"})
    _wire(monkeypatch, server)
    _run(inventory, tags="keycloak,oauth2_proxy", keyset_json=True)
    accounts, system = (c for _, c in server.runs())
    assert "--tags keycloak,oauth2_proxy" in system and "--keyset-json" in system
    assert "--tags" not in accounts


def test_a_failed_first_leg_stops_the_install(monkeypatch, inventory):
    server = FakeServer(key_opens={"ops"})
    server.rc["accounts"] = 1
    ran = _wire(monkeypatch, server)
    assert _run(inventory)[0] == 1
    assert len(server.runs()) == 1 and ran == []


def test_the_passwords_come_back_apart_from_the_log(monkeypatch, inventory):
    server = FakeServer(key_opens={"ops"})
    server.prints["system"] = ["bootstrap", install.KEYSET_BEGIN, "SAVE THESE",
                               install.KEYSET_END, "converge"]
    _wire(monkeypatch, server)
    _, lines, blocks = _run(inventory)
    assert blocks == [["SAVE THESE"]]
    assert "bootstrap" in lines and "converge" in lines and "SAVE THESE" not in lines


def test_the_outside_checks_run_last_and_count(monkeypatch, inventory):
    server = FakeServer(key_opens={"ops"})
    ran = _wire(monkeypatch, server, checks_pass=False)
    assert _run(inventory)[0] == install.VALIDATION_FAILED
    assert ran == [("ops", "203.0.113.10")]
    ran.clear()
    _wire(monkeypatch, server)
    assert _run(inventory, outside_checks=False)[0] == 0
    assert ran == []


def test_the_address_is_the_runs_then_the_inventorys(inventory):
    assert install.Server.of(inventory).host == "203.0.113.10"
    assert install.Server.of(inventory, "100.64.0.5").host == "100.64.0.5"
    (inventory / "hosts.yml").write_text(
        "all:\n  children:\n    vps:\n      hosts:\n        clone1:\n"
        "          ansible_host: 10.0.0.7\n")
    server = install.Server.of(inventory)
    assert (server.name, server.host) == ("clone1", "10.0.0.7")
    with (inventory / ".env").open("a") as fh:
        fh.write("HOST_SSH_ADDRESS=100.64.0.9\n")
    (inventory / "hosts.yml").unlink()
    server = install.Server.of(inventory)
    assert (server.name, server.host) == ("clientco1", "100.64.0.9")


def test_the_markers_are_the_servers():
    script = (ANSIBLE / "install-host.sh").read_text()
    assert f'KEYSET_BEGIN="{install.KEYSET_BEGIN}"' in script
    assert f'KEYSET_END="{install.KEYSET_END}"' in script
