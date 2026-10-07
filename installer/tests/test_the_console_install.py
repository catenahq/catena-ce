"""`catena-installer install`, the console mode the bench and operators run:
one input file for a new server, the answers it asks for when there is a
terminal, and nothing asked when there is none.

Run: uv run pytest installer/tests/test_the_console_install.py
"""
from __future__ import annotations

import pytest

from catena_installer import __main__ as main_mod, install, remote, tree


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "id_ed25519"
    path.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA catena\n")
    return path


def _input(tmp_path, key, **extra) -> str:
    lines = [f"HOST_PUBLIC_IP: 203.0.113.10", "ADMIN_EMAIL: admin@client.test",
             f"SSH_PRIVATE_KEY: {key}", "host_name: clientco1",
             *(f"{k}: {v}" for k, v in extra.items())]
    path = tmp_path / "install.yaml"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _args(tmp_path, *more: str):
    return main_mod.build_parser().parse_args(
        ["install", "--inventory-path", str(tmp_path / "clientco"), "--no-confirm", *more])


def test_the_input_file_seeds_the_inventory_and_reaches_the_install(tmp_path, key, monkeypatch):
    seen = {}

    def run(inv_dir, opts, out):
        seen.update(inv_dir=inv_dir, opts=opts)
        return 0

    monkeypatch.setattr(install, "run", run)
    path = _input(tmp_path, key, host_initial_password="provider-pw",
                  admin_password="a-pinned-admin-password")
    args = _args(tmp_path, "-i", path, "--release", "v1.2.3")
    assert main_mod.cmd_install(args) == 0
    inv = tmp_path / "clientco"
    env = tree.seed().read_existing_env(inv / ".env")
    assert env["HOST_PUBLIC_IP"] == "203.0.113.10"
    assert "clientco1" in (inv / "hosts.yml").read_text()
    opts = seen["opts"]
    assert (opts.password, opts.release) == ("provider-pw", "v1.2.3")
    assert opts.adopt == {"admin_password": "a-pinned-admin-password"}
    for path in inv.rglob("*"):
        text = path.read_text(errors="replace") if path.is_file() else ""
        assert "provider-pw" not in text and "a-pinned-admin-password" not in text


def test_with_no_terminal_a_changed_host_key_stops_the_install(tmp_path, key, monkeypatch, capsys):
    def run(inv_dir, opts, out):
        raise remote.HostKeyChanged("203.0.113.10", ["old"], "new")

    monkeypatch.setattr(install, "run", run)
    assert main_mod.cmd_install(_args(tmp_path, "-i", _input(tmp_path, key))) == 1
    assert "--reinstalled" in capsys.readouterr().out


def test_with_no_terminal_a_missing_password_stops_the_install(tmp_path, key, monkeypatch):
    def run(inv_dir, opts, out):
        raise install.PasswordNeeded("no key opens it")

    monkeypatch.setattr(install, "run", run)
    assert main_mod.cmd_install(_args(tmp_path, "-i", _input(tmp_path, key))) == 1


def test_the_passwords_print_between_the_markers(capsys):
    main_mod._print_keyset(["SAVE THESE"])
    assert capsys.readouterr().out.splitlines() == [
        install.KEYSET_BEGIN, "SAVE THESE", install.KEYSET_END]
