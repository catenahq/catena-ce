"""Unit tests for the `catena-cli` CLI wrapper: command wiring."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
CATENA_PATH = ANSIBLE_DIR / "catena_cli.py"


@pytest.fixture(scope="module")
def cli():
    # Loaded from its path rather than imported by name: the tests run from
    # tests/unit and ansible/ is not on sys.path there.
    spec = importlib.util.spec_from_file_location("catena_cli", CATENA_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("catena_cli", mod)
    spec.loader.exec_module(mod)
    return mod


def test_install_chain_order(cli):
    """Fresh install runs bootstrap, then the converge, then validate. No leg
    joins a tailnet or checks a tailnet credential: the install takes none."""
    assert cli.INSTALL_CHAIN == ("bootstrap", "converge", "validate")


def _stage_of(cmd):
    """The playbook stem of an ansible-playbook argv (the .yml arg)."""
    pb = next(a for a in cmd if a.endswith(".yml"))
    return pb.rsplit("/", 1)[-1].removesuffix(".yml")


# ---- transient secret threading ----

def test_run_deploy_chain_threads_global_extra_on_every_stage(cli, monkeypatch, tmp_path):
    """The transient adopt file (`-e @file`) rides EVERY stage so the on-box
    loader adopts the vendor creds; bootstrap also carries its own creds."""
    from helpers import bootstrap_output

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])
    cli._run_deploy_chain(
        tmp_path, ("bootstrap", "converge", "validate"),
        bootstrap_extra=["-e", "@boot"], global_extra=["-e", "@secrets"],
    )
    pb = [c for c in calls if c and c[0] == "ansible-playbook"]
    assert pb
    for c in pb:
        assert "@secrets" in " ".join(c), _stage_of(c)
    boot = next(c for c in pb if _stage_of(c) == "bootstrap")
    assert "@boot" in " ".join(boot)


def test_the_chain_applies_the_install_address_after_bootstrap(cli, monkeypatch, tmp_path):
    """Bootstrap emits the address it reached the host on, and the chain folds
    it in before the next leg, which would otherwise dial the placeholder."""
    from helpers import bootstrap_output

    events: list[str] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: events.append(
        f"run {_stage_of(cmd)} {' '.join(cmd)}"))
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory",
                        lambda p: events.append("apply") or [])
    cli._run_deploy_chain(tmp_path, cli.INSTALL_CHAIN, bootstrap_extra=None)
    order = [e.split()[1] if e.startswith("run ") else e for e in events]
    assert order == ["bootstrap", "apply", "converge", "validate"], order
    assert not any("catena_lockdown" in e for e in events)


def test_no_provider_password_is_asked_when_the_key_already_opens(cli, monkeypatch, tmp_path):
    """Many providers install the key at order time; asking for a password the
    install will not use is a question with no reason."""
    from helpers import install_key

    (tmp_path / ".env").write_text("HOST_PUBLIC_IP=203.0.113.10\nOPS_USER=ops\n")
    tried = []
    monkeypatch.setattr(install_key, "_key_already_works",
                        lambda host, user, key, port=22: tried.append(user) or user == "debian")
    monkeypatch.setattr(cli.getpass, "getpass",
                        lambda prompt: pytest.fail("asked for a password"))
    assert cli._prompt_provider_password(tmp_path, "debian") == ""
    assert tried == ["debian"]


def test_the_provider_password_is_asked_when_the_key_does_not_open(cli, monkeypatch, tmp_path):
    from helpers import install_key

    (tmp_path / ".env").write_text("HOST_PUBLIC_IP=203.0.113.10\n")
    monkeypatch.setattr(install_key, "_key_already_works",
                        lambda host, user, key, port=22: False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "provider-pw")
    assert cli._prompt_provider_password(tmp_path, "debian") == "provider-pw"


def test_playbook_cmd_shape(cli):
    cmd = cli.playbook_cmd("prod", "converge")
    assert cmd[0] == "ansible-playbook"
    assert "-i" in cmd
    inv = cmd[cmd.index("-i") + 1]
    assert inv.endswith("inventory/prod")
    assert cmd[-1].endswith("playbooks/converge.yml")


def test_playbook_cmd_extra_args(cli):
    cmd = cli.playbook_cmd("prod", "bootstrap", ["--limit", "prod1-bootstrap"])
    assert cmd[-2:] == ["--limit", "prod1-bootstrap"]


def _install_args(cli, *extra):
    return cli.build_parser().parse_args(["install", "--inventory", "test", *extra])


def test_tags_scope_the_converge_leg_only(cli):
    """`catena-cli install --tags a,b` scopes the converge to those roles (e.g.
    re-apply only keycloak,oauth2_proxy after rotating a secret); bootstrap and
    the validation always run whole."""
    ns = _install_args(cli, "--tags", "keycloak,oauth2_proxy")
    assert cli._stage_extra("converge", ns) == ["--tags", "keycloak,oauth2_proxy"]
    assert cli._stage_extra("bootstrap", ns) == []
    assert cli._stage_extra("validate", ns) == []


def test_no_option_adds_nothing_to_any_leg(cli):
    ns = _install_args(cli)
    for stage in (*cli.INSTALL_CHAIN, "show-keyset"):
        assert cli._stage_extra(stage, ns) == [], stage


def test_an_address_reaches_every_leg_for_this_run_only(cli, monkeypatch, tmp_path):
    """Once the panel's Lockdown has closed public SSH, the host answers on its
    tailnet address. Every leg dials it, and the inventory keeps the address
    it had: folding the run's address into hosts.yml would make it permanent."""
    from helpers import bootstrap_output

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory",
                        lambda p: pytest.fail("a one-run address was kept"))
    ns = _install_args(cli, "--address", "100.64.0.7", "--tags", "keycloak")
    cli._run_deploy_chain(tmp_path, cli.INSTALL_CHAIN, bootstrap_extra=None, args=ns)
    assert [_stage_of(c) for c in calls] == list(cli.INSTALL_CHAIN)
    for c in calls:
        assert c[c.index("-e") + 1] == "ansible_host=100.64.0.7", _stage_of(c)
    assert calls[1][calls[1].index("--tags") + 1] == "keycloak"
    assert "--tags" not in calls[0] and "--tags" not in calls[2]
    assert cli._stage_extra("show-keyset", ns) == ["-e", "ansible_host=100.64.0.7"]


@pytest.mark.parametrize("bad", ["", "1.2.3.4; rm -rf /", "-e", "a b"])
def test_an_address_that_is_not_one_is_refused(cli, bad):
    with pytest.raises(SystemExit):
        _install_args(cli, "--address", bad)


def test_the_ssh_address_is_the_run_s_then_the_env_s_then_the_public_ip(cli):
    env = {"HOST_PUBLIC_IP": "203.0.113.10"}
    assert cli._ssh_address(env) == "203.0.113.10"
    env["HOST_SSH_ADDRESS"] = "100.64.0.5"
    assert cli._ssh_address(env) == "100.64.0.5"
    assert cli._ssh_address(env, "100.64.0.7") == "100.64.0.7"


def test_the_key_probe_dials_the_ssh_address(cli, monkeypatch):
    from helpers import install_key

    dialled: list[str] = []
    monkeypatch.setattr(install_key, "_key_already_works",
                        lambda host, user, key, port=22: dialled.append(host) or True)
    env = {"HOST_PUBLIC_IP": "203.0.113.10", "HOST_SSH_ADDRESS": "100.64.0.5"}
    assert cli._key_already_opens(env, "debian") == "debian"
    assert cli._key_already_opens(env, "debian", "100.64.0.7") == "debian"
    assert dialled == ["100.64.0.5", "100.64.0.7"]


def test_an_address_is_refused_for_an_inventory_of_several_servers(cli, tmp_path, monkeypatch):
    """One address for every leg needs one server to give it to."""
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    (inv_dir / "hosts.yml").write_text(
        "all:\n  children:\n    vps:\n      hosts:\n"
        "        a: {ansible_host: 0.0.0.0}\n        b: {ansible_host: 0.0.0.0}\n")
    monkeypatch.setattr(cli, "_preflight_checks", lambda *a, **k: None)
    monkeypatch.setattr(cli, "inventory_path", lambda name: inv_dir)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli, "_run_deploy_chain",
                        lambda *a, **k: pytest.fail("deployed with an ambiguous address"))
    with pytest.raises(SystemExit):
        cli.cmd_install(_install_args(cli, "--address", "100.64.0.7"))
    assert cli._vps_hosts(inv_dir) == ["a", "b"]


def _changed_key(cli, monkeypatch, tmp_path, *flags, tty=False, answer=""):
    """Settle a server at 203.0.113.10 that presents another host key; returns
    what was forgotten."""
    from helpers import host_key

    (tmp_path / ".env").write_text("HOST_PUBLIC_IP=203.0.113.10\n")
    forgotten: list[tuple] = []
    monkeypatch.setattr(host_key, "changed", lambda host, port: (
        ["ssh-ed25519 SHA256:old"], ["ssh-ed25519 SHA256:new"]))
    monkeypatch.setattr(host_key, "forget", lambda host, port: forgotten.append((host, port)))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    cli._settle_host_key(tmp_path, _install_args(cli, *flags))
    return forgotten


def test_a_changed_host_key_stops_an_unattended_run(cli, monkeypatch, tmp_path, capsys):
    """Nobody can say the server was reinstalled, so nothing reaches it."""
    with pytest.raises(SystemExit):
        _changed_key(cli, monkeypatch, tmp_path, "--no-confirm")
    err = capsys.readouterr().err
    assert "SHA256:old" in err and "SHA256:new" in err and "--reinstalled" in err


def test_reinstalled_forgets_the_old_key(cli, monkeypatch, tmp_path):
    assert _changed_key(cli, monkeypatch, tmp_path, "--no-confirm",
                        "--reinstalled") == [("203.0.113.10", 22)]


def test_a_changed_host_key_is_asked_about(cli, monkeypatch, tmp_path):
    assert _changed_key(cli, monkeypatch, tmp_path, tty=True,
                        answer="y") == [("203.0.113.10", 22)]
    with pytest.raises(SystemExit):
        _changed_key(cli, monkeypatch, tmp_path, tty=True, answer="")


def test_the_trusted_key_settles_nothing(cli, monkeypatch, tmp_path):
    from helpers import host_key

    (tmp_path / ".env").write_text("HOST_PUBLIC_IP=203.0.113.10\nHOST_SSH_PORT=2222\n")
    asked: list[tuple] = []
    monkeypatch.setattr(host_key, "changed",
                        lambda host, port: asked.append((host, port)) or ([], []))
    monkeypatch.setattr(host_key, "forget", lambda *a: pytest.fail("forgot a key"))
    cli._settle_host_key(tmp_path, _install_args(cli, "--address", "100.64.0.7"))
    assert asked == [("100.64.0.7", 2222)]


def test_the_host_key_is_settled_before_the_password_is_asked(cli, tmp_path, monkeypatch):
    """The key probe and the provider password go to whatever answers."""
    order = []
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    monkeypatch.setattr(cli, "_preflight_checks", lambda *a, **k: None)
    monkeypatch.setattr(cli, "inventory_path", lambda name: inv_dir)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli, "_settle_host_key", lambda inv, args: order.append("host key"))
    monkeypatch.setattr(cli, "_bootstrap_extra_vars",
                        lambda *a, **k: order.append("password") or ([], None))
    monkeypatch.setattr(cli, "ensure_collections", lambda: order.append("collections"))
    monkeypatch.setattr(cli, "_run_deploy_chain", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_show_dr_keyset", lambda inv, extra, as_json: [])
    assert cli.cmd_install(_install_args(cli)) == 0
    assert order == ["host key", "password", "collections"]


def test_check_prereqs_reports_missing(cli, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    missing = cli.check_prereqs(("ansible-playbook", "ansible"))
    assert set(missing) == {"ansible-playbook", "ansible"}


def test_check_prereqs_all_present(cli, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    assert cli.check_prereqs(("ansible",)) == []


def test_required_binaries_are_ansible_only(cli):
    assert cli.REQUIRED_BINARIES == ("ansible-playbook", "ansible")


def test_preflight_passes_with_core_binaries(cli, monkeypatch):
    """Every command runs against a plaintext inventory; only ansible is
    required."""
    present = set(cli.REQUIRED_BINARIES)
    monkeypatch.setattr(
        cli.shutil, "which",
        lambda name: ("/usr/bin/" + name) if name in present else None,
    )
    cli._preflight_checks()  # must NOT raise


def test_ensure_collections_uses_writable_override_path(cli, monkeypatch, tmp_path):
    """On a read-only checkout, ANSIBLE_COLLECTIONS_PATH redirects the galaxy
    install to a writable dir (ansible reads the same var), so the CLI can run
    without writing into the :ro checkout's collections/ dir."""
    target = tmp_path / "colls"  # absent -> triggers install
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", str(target))
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    cli.ensure_collections()
    assert len(calls) == 1, calls
    cmd = calls[0]
    assert cmd[:3] == ["ansible-galaxy", "collection", "install"]
    assert cmd[cmd.index("-p") + 1] == str(target)


def test_collections_dir_matches_ansible_cfg(cli):
    """The in-tree galaxy install target is READ from ansible.cfg, so the
    install lands where ansible looks."""
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(cli.ANSIBLE_DIR / "ansible.cfg")
    assert cli.COLLECTIONS_DIR == cfg.get("defaults", "collections_path")


def test_ensure_collections_installs_where_ansible_reads(cli, monkeypatch, tmp_path):
    """With no override, the install target is ANSIBLE_DIR/<collections_path>."""
    monkeypatch.delenv("ANSIBLE_COLLECTIONS_PATH", raising=False)
    monkeypatch.setattr(cli, "ANSIBLE_DIR", tmp_path)
    (tmp_path / "requirements.yml").write_text("collections: []\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    cli.ensure_collections()
    assert len(calls) == 1, calls
    cmd = calls[0]
    assert cmd[cmd.index("-p") + 1] == str(tmp_path / cli.COLLECTIONS_DIR)


def test_ensure_collections_skips_a_tree_that_holds_the_pins(cli, monkeypatch, tmp_path):
    import hashlib

    existing = tmp_path / "colls"
    existing.mkdir()
    req = (cli.ANSIBLE_DIR / "requirements.yml").read_bytes()
    (existing / ".requirements.sha256").write_text(hashlib.sha256(req).hexdigest())
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", str(existing))
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    cli.ensure_collections()
    assert calls == []


def test_a_tree_installed_from_other_pins_is_rebuilt(cli, monkeypatch, tmp_path):
    """A tree holding a collection the pins do not name, or one at another
    version than they pin, is rebuilt to exactly the pinned set."""
    monkeypatch.delenv("ANSIBLE_COLLECTIONS_PATH", raising=False)
    monkeypatch.setattr(cli, "ANSIBLE_DIR", tmp_path)
    (tmp_path / "requirements.yml").write_text("collections: []\n")
    stale = tmp_path / cli.COLLECTIONS_DIR / "ansible_collections" / "community" / "unpinned"
    stale.mkdir(parents=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    cli.ensure_collections()
    assert len(calls) == 1 and "--force" in calls[0]
    assert not stale.exists()
    calls.clear()
    cli.ensure_collections()
    assert calls == []


def test_bootstrap_extra_vars_writes_secret_to_file_not_argv(cli, tmp_path):
    import yaml

    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    (inv_dir / ".env").write_text("HOST_INITIAL_USER=debian\n")
    iy = tmp_path / "install.yaml"
    iy.write_text(
        "inventory: test\n"
        "host_initial_password: s3cr3t-provider-pw\n"
    )
    extra, tmp = cli._bootstrap_extra_vars(inv_dir, str(iy))
    try:
        # The provider password is referenced as -e @file, never inline on
        # argv (would otherwise leak via `ps` and the printed command).
        assert extra[0] == "-e"
        assert extra[1].startswith("@")
        assert "s3cr3t-provider-pw" not in " ".join(extra)
        data = yaml.safe_load(tmp.read_text())
        # install.yaml has no host_initial_user in this case, so
        # bootstrap_initial_user falls back to the inventory's own .env.
        assert data["bootstrap_initial_user"] == "debian"
        assert data["bootstrap_root_password"] == "s3cr3t-provider-pw"
        # 0600 so the provider password is not world-readable on disk.
        assert (tmp.stat().st_mode & 0o777) == 0o600
    finally:
        tmp.unlink(missing_ok=True)


def test_bootstrap_extra_vars_install_yaml_initial_user_overrides_env(cli, tmp_path):
    """An answers file describes the box about to be built, so its
    host_initial_user wins over an inventory .env written for another one."""
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    (inv_dir / ".env").write_text("HOST_INITIAL_USER=debian\n")
    iy = tmp_path / "install.yaml"
    iy.write_text("host_initial_user: root\n")
    extra, tmp = cli._bootstrap_extra_vars(inv_dir, str(iy))
    try:
        import yaml
        data = yaml.safe_load(tmp.read_text())
        assert data["bootstrap_initial_user"] == "root"
    finally:
        tmp.unlink(missing_ok=True)


def test_bootstrap_extra_vars_answers_the_password_prompt_even_when_blank(cli, tmp_path):
    """No inventory, no -i, nothing to prompt with: the password name is still
    emitted. Leaving it out is exactly what lets bootstrap.yml's vars_prompt
    stop the deploy chain to ask for it once the operator has walked away."""
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    extra, tmp = cli._bootstrap_extra_vars(inv_dir, None)
    try:
        import yaml
        assert extra == ["-e", f"@{tmp}"]
        assert yaml.safe_load(tmp.read_text()) == {"bootstrap_root_password": ""}
    finally:
        tmp.unlink(missing_ok=True)


def test_bootstrap_extra_vars_reads_initial_user_without_install_yaml(cli, tmp_path):
    """A self-hoster driving an already-filled-in inventory with no -i still
    gets the correct bootstrap_initial_user injected."""
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    (inv_dir / ".env").write_text("HOST_INITIAL_USER=ubuntu\n")
    extra, tmp = cli._bootstrap_extra_vars(inv_dir, None)
    try:
        import yaml
        data = yaml.safe_load(tmp.read_text())
        assert data == {"bootstrap_initial_user": "ubuntu",
                        "bootstrap_root_password": ""}
    finally:
        tmp.unlink(missing_ok=True)


def test_bootstrap_extra_vars_prompts_for_the_provider_password(cli, tmp_path, monkeypatch):
    """Asked here, before the first playbook -- not by bootstrap.yml partway
    through the chain."""
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    (inv_dir / ".env").write_text("HOST_INITIAL_USER=debian\nHOST_PUBLIC_IP=198.51.100.9\n")
    asked = []

    def fake_getpass(prompt):
        asked.append(prompt)
        return "provider-pw"

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    extra, tmp = cli._bootstrap_extra_vars(inv_dir, None, prompt_password=True)
    try:
        import yaml
        assert len(asked) == 1
        assert yaml.safe_load(tmp.read_text()) == {
            "bootstrap_initial_user": "debian",
            "bootstrap_root_password": "provider-pw",
        }
    finally:
        tmp.unlink(missing_ok=True)


def test_bootstrap_extra_vars_does_not_prompt_without_a_tty(cli, tmp_path, monkeypatch):
    """No TTY must not hang, and must not leave the name out either."""
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()

    def boom(prompt):
        raise AssertionError("prompted with no TTY")

    monkeypatch.setattr(cli.getpass, "getpass", boom)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    extra, tmp = cli._bootstrap_extra_vars(inv_dir, None, prompt_password=True)
    try:
        import yaml
        assert yaml.safe_load(tmp.read_text()) == {"bootstrap_root_password": ""}
    finally:
        tmp.unlink(missing_ok=True)


def test_install_password_prompt_precedes_the_deploy_chain(cli, tmp_path, monkeypatch):
    """The ordering the rule is about: every question answered before the
    first playbook runs."""
    order = []
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()

    monkeypatch.setattr(cli, "_preflight_checks", lambda *a, **k: None)
    monkeypatch.setattr(cli, "inventory_path", lambda name: inv_dir)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))

    def fake_bootstrap_extra(inv, input_path, *, prompt_password=False, address=None):
        order.append(("prompt", prompt_password))
        return [], None

    def fake_chain(inv, chain, **kw):
        order.append(("chain", *chain))

    monkeypatch.setattr(cli, "_bootstrap_extra_vars", fake_bootstrap_extra)
    monkeypatch.setattr(cli, "_run_deploy_chain", fake_chain)
    monkeypatch.setattr(cli, "_show_dr_keyset",
                        lambda inv, extra, as_json: order.append(("keyset",)) or [])

    ns = cli.build_parser().parse_args(["install", "--inventory", "prod"])
    assert cli.cmd_install(ns) == 0
    assert order == [("prompt", True), ("chain", "bootstrap"), ("keyset",),
                     ("chain", "converge", "validate")]


_HANDED_OVER = {"banner": "Admin password: pw\n", "admin_password": "pw",
                "console_recovery_password": "cpw",
                "journal_verification_key": "fss-key"}


def _install_with_keyset(cli, tmp_path, monkeypatch, keysets, *flags):
    inv_dir = tmp_path / "inv"
    inv_dir.mkdir()
    monkeypatch.setattr(cli, "_preflight_checks", lambda *a, **k: None)
    monkeypatch.setattr(cli, "inventory_path", lambda name: inv_dir)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli, "_bootstrap_extra_vars", lambda *a, **k: ([], None))
    monkeypatch.setattr(cli, "_run_deploy_chain", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_show_dr_keyset", lambda inv, extra, as_json: keysets)
    ns = cli.build_parser().parse_args(["install", "--inventory", "prod", *flags])
    assert cli.cmd_install(ns) == 0


def test_the_passwords_are_printed_again_when_the_install_ends(cli, tmp_path, monkeypatch, capsys):
    """Shown after bootstrap, they scroll away under the converge in a
    terminal, so the install ends on the same block."""
    _install_with_keyset(cli, tmp_path, monkeypatch, [_HANDED_OVER])
    err = capsys.readouterr().err
    tail = err[err.rindex(cli.KEYSET_BEGIN):]
    assert tail.startswith(f"{cli.KEYSET_BEGIN}\nAdmin password: pw\n{cli.KEYSET_END}")


def test_the_graphical_installer_gets_the_values_as_json(cli, tmp_path, monkeypatch, capsys):
    """--keyset-json frames each host's values, without the block, so the page
    can show each one on its own."""
    import json

    _install_with_keyset(cli, tmp_path, monkeypatch, [_HANDED_OVER], "--keyset-json")
    err = capsys.readouterr().err
    framed = err[err.rindex(cli.KEYSET_BEGIN):].splitlines()
    assert framed[2] == cli.KEYSET_END
    assert json.loads(framed[1]) == {k: v for k, v in _HANDED_OVER.items()
                                     if k != "banner"}


def test_no_block_is_printed_at_the_end_when_none_was_shown(cli, tmp_path, monkeypatch, capsys):
    _install_with_keyset(cli, tmp_path, monkeypatch, [])
    assert cli.KEYSET_BEGIN not in capsys.readouterr().err


def test_show_dr_keyset_prints_the_handed_over_block_and_deletes_it(cli, tmp_path, monkeypatch, capsys):
    """The play writes one JSON file per host into the directory it is given;
    the CLI prints the blocks framed, returns what it read, and leaves no file
    behind."""
    import json

    seen = {}

    def fake_run(cmd, *a, **k):
        out = Path(next(a for a in cmd if a.startswith("keyset_out=")).split("=", 1)[1])
        seen["dir"], seen["cmd"] = out, cmd
        (out / "host1").write_text(json.dumps(_HANDED_OVER))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._show_dr_keyset(tmp_path, ["-e", "@adopt"]) == [_HANDED_OVER]
    assert f"{cli.KEYSET_BEGIN}\nAdmin password: pw\n{cli.KEYSET_END}" in capsys.readouterr().err
    assert _stage_of(seen["cmd"]) == "show-keyset"
    assert "@adopt" in seen["cmd"]
    assert not seen["dir"].exists()


def test_show_dr_keyset_failing_does_not_stop_the_install(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=2))
    assert cli._show_dr_keyset(tmp_path) == []
    err = capsys.readouterr().err
    assert cli.KEYSET_BEGIN not in err and "could not show the passwords" in err


def test_the_verbs_are_init_install_and_uninstall(cli):
    """The CLI writes an inventory and reaches the server over SSH, and
    `install` is the one verb that (re)applies it. Backups, the tunnel, the
    tailnet and the passwords are the panel's, on the host."""
    assert cli.KNOWN_COMMANDS == ("init", "install", "uninstall")


# ---- init: a new inventory to fill in ----

def _init(cli, monkeypatch, tmp_path, argv):
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / name)
    ns = cli.build_parser().parse_args(argv)
    return ns.func(ns)


def test_init_writes_an_env_with_every_default(cli, monkeypatch, tmp_path, capsys):
    import seed

    assert _init(cli, monkeypatch, tmp_path, ["init", "--inventory", "newco"]) == 0
    env = seed.read_existing_env(tmp_path / "newco" / ".env")
    assert env == dict(seed.ENV_KEYS)
    assert "catena-cli install --inventory newco" in capsys.readouterr().err


def test_init_refuses_an_inventory_that_exists(cli, monkeypatch, tmp_path):
    (tmp_path / "clientco").mkdir()
    with pytest.raises(SystemExit):
        _init(cli, monkeypatch, tmp_path, ["init", "--inventory", "clientco"])
    assert not (tmp_path / "clientco" / ".env").exists()


def test_init_refuses_a_name_that_is_not_one(cli, monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _init(cli, monkeypatch, tmp_path, ["init", "--inventory", "Has Space"])


def test_init_takes_a_path_outside_the_checkout(cli, monkeypatch, tmp_path, capsys):
    target = tmp_path / "ops-inv" / "clientA"
    assert _init(cli, monkeypatch, tmp_path,
                 ["init", "--inventory-path", str(target)]) == 0
    assert (target / ".env").is_file()
    assert f"--inventory-path {target}" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["backup", "validate", "rotate-tunnel",
                                  "rotate-tailscale", "show-keyset", "converge"])
def test_the_panels_operations_are_not_cli_verbs(cli, verb):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([verb, "--inventory", "prod"])


# ---- --inventory-path: drive an inventory OUTSIDE the checkout ----
# (an operator pointing the CLI at an ops-side inventory driven by
# external automation).

def test_resolve_inventory_name_resolves_under_checkout(cli):
    ns = cli.build_parser().parse_args(["uninstall", "--inventory", "prod"])
    inv = cli.resolve_inventory(ns)
    assert inv == cli.inventory_path("prod")
    assert str(inv).endswith("inventory/prod")


def test_resolve_inventory_path_is_used_verbatim(cli, tmp_path):
    ext = tmp_path / "ops" / "inventory" / "clientA"
    ns = cli.build_parser().parse_args(["uninstall", "--inventory-path", str(ext)])
    assert cli.resolve_inventory(ns) == ext


def test_playbook_cmd_accepts_a_path_directly(cli, tmp_path):
    from pathlib import Path

    ext = Path(tmp_path) / "clientA"
    cmd = cli.playbook_cmd(ext, "converge")
    assert cmd[cmd.index("-i") + 1] == str(ext)
    assert cmd[-1].endswith("playbooks/converge.yml")


def test_inventory_path_threads_to_ansible_playbook_i_flag(cli, monkeypatch, tmp_path):
    """A --inventory-path run passes that exact dir to `ansible-playbook -i` on
    every stage -- so the operator's ops-side inventory is what ansible reads."""
    from helpers import bootstrap_output

    ext = tmp_path / "ops-inv" / "clientA"
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])

    ns = cli.build_parser().parse_args(["uninstall", "--inventory-path", str(ext)])
    assert ns.func(ns) == 0
    pb_calls = [c for c in calls if c and c[0] == "ansible-playbook"]
    assert pb_calls
    for c in pb_calls:
        assert c[c.index("-i") + 1] == str(ext)


def test_inventory_and_path_are_mutually_exclusive(cli):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["uninstall", "--inventory", "prod", "--inventory-path", "/tmp/x"]
        )


def test_inventory_required_for_non_install_commands(cli):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["uninstall"])


def test_install_accepts_inventory_path_and_skips_the_name(cli):
    ns = cli.build_parser().parse_args(["install", "--inventory-path", "/tmp/x"])
    assert ns.func is cli.cmd_install
    assert ns.inventory_path == "/tmp/x"
    assert ns.inventory is None


# ---- no-argument menu + `--install` alias ----

def test_bare_invocation_is_not_an_error(cli):
    """Subparsers are not required: bare `catena-cli` parses to command=None (the
    dispatcher then drops into the interactive menu) instead of erroring."""
    ns = cli.build_parser().parse_args([])
    assert ns.command is None


def test_install_flag_is_alias_for_install_subcommand(cli, monkeypatch):
    """`catena-cli --install [flags]` dispatches to cmd_install, same as
    `catena-cli install [flags]`."""
    seen = {}
    monkeypatch.setattr(cli.os, "chdir", lambda p: None)
    monkeypatch.setattr(cli, "cmd_install", lambda ns: (seen.update(ns=ns), 0)[1])
    assert cli.main(["--install", "--inventory", "prod"]) == 0
    assert seen["ns"].func is cli.cmd_install
    assert seen["ns"].inventory == "prod"


def test_interactive_menu_prompts_inventory_before_command(cli, monkeypatch):
    """Inventory first, then the numbered menu -- every command (install
    included) comes back with --inventory attached."""
    answers = iter(["dev", "2"])  # inventory, then 2 == install
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli.interactive_menu() == ["install", "--inventory", "dev"]


def test_interactive_menu_other_command(cli, monkeypatch):
    answers = iter(["dev", "1"])  # inventory, then 1 == init
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli.interactive_menu() == ["init", "--inventory", "dev"]


def test_interactive_menu_inventory_defaults_to_prod(cli, monkeypatch):
    answers = iter(["", "3"])  # blank inventory -> prod, then 3 == uninstall
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli.interactive_menu() == ["uninstall", "--inventory", "prod"]


def test_interactive_menu_defaults_to_install(cli, monkeypatch):
    answers = iter(["dev", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli.interactive_menu() == ["install", "--inventory", "dev"]


def test_interactive_menu_rejects_bad_choice(cli, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "99")
    with pytest.raises(SystemExit):
        cli.interactive_menu()


# --- one argv shape ----------------------------------------------------------
def test_verb_first_shape_is_accepted(cli):
    """`catena-cli <verb> --inventory <name>` is the shape, and nothing rewrites
    it on the way to the parser."""
    argv = ["install", "--inventory", "prod", "--no-confirm"]
    assert cli._reject_bare_inventory(argv) is None


def test_a_leading_flag_is_left_alone(cli):
    assert cli._reject_bare_inventory(["-h"]) is None


def test_leading_inventory_name_is_refused(cli, capsys):
    """An inventory in the verb's position dies with the right shape rather
    than an argparse choice error naming every subcommand."""
    with pytest.raises(SystemExit):
        cli._reject_bare_inventory(["prod", "install"])
    err = capsys.readouterr().err
    assert "--inventory prod" in err
    assert "install" in err


def test_lone_inventory_name_is_refused(cli, capsys):
    """No command to suggest, so the message still has to name the shape."""
    with pytest.raises(SystemExit):
        cli._reject_bare_inventory(["prod"])
    assert "<verb> --inventory prod" in capsys.readouterr().err


def test_main_runs_menu_when_no_args_and_tty(cli, monkeypatch):
    """Bare `catena-cli` on a TTY drives interactive_menu() and dispatches the
    chosen command."""
    seen = {}
    monkeypatch.setattr(cli.os, "chdir", lambda p: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "interactive_menu", lambda: ["uninstall", "--inventory", "dev"])
    monkeypatch.setattr(cli, "cmd_uninstall", lambda ns: (seen.update(ns=ns), 0)[1])
    assert cli.main([]) == 0
    assert seen["ns"].func is cli.cmd_uninstall
    assert seen["ns"].inventory == "dev"


def test_main_no_args_without_tty_dies(cli, monkeypatch):
    """No subcommand and no TTY (e.g. the bench's DEVNULL stdin) is an error,
    never a hung input() prompt."""
    monkeypatch.setattr(cli.os, "chdir", lambda p: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        cli.main([])


def test_main_dispatches_verb_first_shape(cli, monkeypatch):
    """`catena-cli <verb> --inventory <name>` end to end: main() dispatches to the
    right subcommand with the right inventory."""
    seen = {}
    monkeypatch.setattr(cli.os, "chdir", lambda p: None)
    monkeypatch.setattr(cli, "cmd_uninstall", lambda ns: (seen.update(ns=ns), 0)[1])
    assert cli.main(["uninstall", "--inventory", "dev"]) == 0
    assert seen["ns"].func is cli.cmd_uninstall
    assert seen["ns"].inventory == "dev"


@pytest.mark.parametrize("verb", ["restore", "recover", "rollback"])
def test_restores_are_not_cli_verbs(cli, verb):
    """A restore runs on an installed host, from the panel. A CLI verb that
    restored through the converge would be a second path with its own
    marker, hooks and bugs."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([verb, "--inventory", "prod"])
    assert verb not in cli.KNOWN_COMMANDS


def test_main_refuses_inventory_first_shape(cli, monkeypatch):
    """An inventory-first shape is an error, not a silent reinterpretation."""
    monkeypatch.setattr(cli.os, "chdir", lambda p: None)
    with pytest.raises(SystemExit):
        cli.main(["dev", "install"])
