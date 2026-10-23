"""The dispatch path moves only through a proven switch (helpers/dispatch_path.py).

An update may change the path a panel request crosses to become a root action
-- sshd's AcceptEnv, the forced command, the sudo rule, the dispatcher -- and
must never leave the panel without one that works. So the new generation is
installed beside the running one, a dispatch is proven through it, and only
then does the panel's key move; a failed proof at any step leaves a path the
panel could dispatch through.

The switch runs against a temporary root. Its commands are answered by a fake
that behaves as sshd and sudo would from the files the switch wrote: a key
line's command, the sudo rules and env_keep in /etc/sudoers.d (skipping names
with a dot, as sudo does), and the AcceptEnv names sshd loaded at its last
reload. A dispatch that reaches a generation's dispatcher answers the proof as
scripts/catena-admin-runner.sh does.

Run: uv run pytest tests/unit/test_dispatch_path.py
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANSIBLE / "helpers"))

import dispatch_path as dp  # noqa: E402
from ansible_tree import PANEL_HOST_TASKS, walk_tasks  # noqa: E402

NAMES = ["X_FORWARDED_EMAIL", "CATENA_DISPATCH_PAYLOAD", "CATENA_CF_CANDIDATE_TOKEN"]
USER = "catena-admin-runner"
HOME = "/home/catena-admin-runner"
KEYS = f"{HOME}/.ssh/authorized_keys"
KEYDIR = "/etc/catena/admin-ssh"
LINK = "/usr/local/sbin/catena-admin-runner"
PANEL_PUB = "ssh-ed25519 PANELBLOB"
ENTRY = (ANSIBLE / "scripts" / "catena-dispatch-entry.sh").read_text(encoding="utf-8")
DISPATCHER = (ANSIBLE / "scripts" / "catena-admin-runner.sh").read_text(encoding="utf-8")
NOW = 1_700_000_000
PROOF_EXPIRY = "20231114222320Z"
LEGACY_LINE = ('command="/usr/bin/sudo -n /usr/local/sbin/catena-admin-runner '
               '\\"$SSH_ORIGINAL_COMMAND\\"",no-port-forwarding,no-X11-forwarding,'
               f'no-agent-forwarding,no-pty {PANEL_PUB}')
LEGACY_NAMES = ["X_FORWARDED_EMAIL", "CATENA_DISPATCH_PAYLOAD"]


def config(**over) -> dict:
    cfg = dict(runner_user=USER, runner_home=HOME, panel_key_dir=KEYDIR, link=LINK,
               passthrough=list(NAMES), entry=ENTRY, dispatcher=DISPATCHER)
    cfg.update(over)
    return cfg


def _done(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class Fake:
    """sshd, sudo, visudo, systemctl and ssh-keygen, answered from the files
    under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.rc: dict[str, int] = {}
        self.transport_failures = 0
        # (fake, "proof" | "panel", request) -> a result, or None to dispatch.
        self.interfere = None
        self.seen: list[dict] = []
        self.loaded = self.accepted()

    def at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def accepted(self) -> set[str]:
        names: set[str] = set()
        drop_ins = self.at("/etc/ssh/sshd_config.d")
        for f in sorted(drop_ins.glob("*.conf")) if drop_ins.is_dir() else []:
            for line in f.read_text().splitlines():
                if line.strip().startswith("AcceptEnv "):
                    names |= set(line.split()[1:])
        return names

    def sudo(self) -> tuple[set[str], set[str]]:
        allowed: set[str] = set()
        keep: set[str] = set()
        rules = self.at("/etc/sudoers.d")
        for f in sorted(rules.iterdir()) if rules.is_dir() else []:
            if "." in f.name or f.name.endswith("~"):
                continue
            for line in f.read_text().splitlines():
                if "NOPASSWD:" in line:
                    allowed.add(line.split("NOPASSWD:", 1)[1].strip())
                m = re.search(r'env_keep \+= "([^"]*)"', line)
                if m:
                    keep |= set(m.group(1).split())
        return allowed, keep

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        tool = Path(argv[0]).name
        if tool == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("PRIVATE")
            key.with_name(key.name + ".pub").write_text("ssh-ed25519 PROOFBLOB\n")
            return _done(self.rc.get(tool, 0))
        if tool == "systemctl":
            if self.rc.get(tool, 0) == 0:
                self.loaded = self.accepted()
            return _done(self.rc.get(tool, 0), stderr="reload failed")
        if tool in ("visudo", "sshd"):
            return _done(self.rc.get(tool, 0), stderr=f"{tool} refuses it")
        assert tool == "ssh", argv
        return self.ssh(argv)

    def ssh(self, argv: list[str]) -> subprocess.CompletedProcess:
        key = argv[argv.index("-i") + 1]
        request = argv[-1]
        env: dict[str, str] = {}
        for arg in argv:
            if arg.startswith("SetEnv "):
                env = dict(kv.split("=", 1) for kv in arg[len("SetEnv "):].split())
        kind = "proof" if key.startswith(str(self.root)) else "panel"
        keys = (self.at(KEYS).read_text() if self.at(KEYS).exists() else "")
        self.seen.append({"kind": kind, "request": request, "keys": keys,
                          "sudoers": sorted(p.name for p in self.at("/etc/sudoers.d").iterdir()),
                          "loaded": set(self.loaded)})
        if self.transport_failures:
            self.transport_failures -= 1
            return _done(255, stderr="ssh: connect to host 127.0.0.1 port 22: Connection refused")
        if self.interfere and (r := self.interfere(self, kind, request)):
            return r
        pub = Path(key + ".pub") if kind == "proof" else self.at(key + ".pub")
        blob = pub.read_text().split()[1]
        line = next((ln for ln in keys.splitlines()
                     if not ln.startswith("#") and blob in ln.split()), None)
        if line is None:
            return _done(255, stderr="Permission denied (publickey).")
        command = re.search(r'command="((?:[^"\\]|\\.)*)"', line).group(1)
        m = re.fullmatch(re.escape(dp.GENERATIONS) + r"/([0-9a-f]{12})/entry", command)
        if not m:
            return _done(1, stderr=f"catena-admin-runner: unknown action '{request}'")
        gen = m.group(1)
        if not self.at(command).is_file():
            return _done(127, stderr="entry: No such file or directory")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", request):
            return _done(2, stderr="catena-dispatch-entry: refusing 'x': ...")
        dispatcher = dp.dispatcher_path(gen)
        allowed, keep = self.sudo()
        if dispatcher not in allowed:
            return _done(1, stderr="sudo: a password is required")
        arrived = {n for n in env if n in self.loaded and n in keep}
        payload = env["CATENA_DISPATCH_PAYLOAD"] if "CATENA_DISPATCH_PAYLOAD" in arrived else ""
        out = [f"dispatch-path-proof {gen}"]
        if payload:
            out += [f"arrived {n}" for n in sorted(arrived) if env[n] == payload]
        return _done(0, stdout="\n".join(out) + "\n")


def tree(root: Path) -> dict[str, object]:
    """Every file and link under root, /run aside: content or link target."""
    out: dict[str, object] = {}
    for p in sorted(root.rglob("*")):
        rel = "/" + str(p.relative_to(root))
        if rel.startswith("/run"):
            continue
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_file():
            out[rel] = p.read_bytes()
    return out


def ids_of(asked: list[str], uid_shift: int = 0):
    """The uid and gid of an account, as the test's own: a test cannot chown to
    another one. The accounts asked about are recorded."""
    def ids(user: str) -> tuple[int, int]:
        asked.append(user)
        return os.getuid() + uid_shift, os.getgid()
    return ids


@pytest.fixture
def host(tmp_path):
    for d in ("run", HOME.lstrip("/") + "/.ssh", KEYDIR.lstrip("/"), "etc/ssh/sshd_config.d",
              "etc/sudoers.d", "usr/local/sbin"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / KEYDIR.lstrip("/") / "id_ed25519").write_text("PANEL PRIVATE")
    (tmp_path / KEYDIR.lstrip("/") / "id_ed25519.pub").write_text(PANEL_PUB + "\n")
    fake = Fake(tmp_path)
    asked: list[str] = []
    sleeps: list[float] = []
    h = dp.Host(str(tmp_path), run=fake, ids=ids_of(asked), sleep=sleeps.append,
                now=lambda: NOW)
    h.fake = fake  # type: ignore[attr-defined]
    h.asked = asked  # type: ignore[attr-defined]
    h.sleeps = sleeps  # type: ignore[attr-defined]
    return h


LEGACY_SUDOERS = (f"Defaults:{USER} !requiretty\n"
                  f'Defaults:{USER} env_keep += "{" ".join(LEGACY_NAMES)}"\n'
                  f"{USER} ALL=(root) NOPASSWD: {LINK}\n")
LEGACY_ACCEPTENV = f"Match User {USER}\n    AcceptEnv {' '.join(LEGACY_NAMES)}\nMatch all\n"


def legacy(host) -> None:
    """A host on the path an install writes with no generation: the forced
    command runs sudo on /usr/local/sbin/catena-admin-runner, a regular file."""
    fake = host.fake
    fake.at(KEYS).write_text(f"# managed by the install\n\n{LEGACY_LINE}\n")
    fake.at(dp.SUDOERS).write_text(LEGACY_SUDOERS)
    fake.at(dp.ACCEPTENV).write_text(LEGACY_ACCEPTENV)
    fake.at(LINK).write_text("#!/bin/bash\n# the install's dispatcher\n")
    fake.loaded = fake.accepted()


def fresh_host(host) -> dp.Host:
    """The same filesystem, a new run: nothing recorded as written yet."""
    h = dp.Host(str(host.root), run=host.fake, ids=ids_of([]), sleep=host.sleeps.append,
                now=lambda: NOW)
    h.fake = host.fake  # type: ignore[attr-defined]
    return h


def on_generation(host, cfg: dict) -> str:
    """A host already on the generation `cfg` describes."""
    result = dp.switch(cfg, host)
    assert result["ok"], result
    host.fake.calls.clear()
    host.fake.seen.clear()
    return result["generation"]


def assert_on(host, cfg: dict) -> None:
    gen = dp.generation(cfg)
    fake = host.fake
    keys = fake.at(KEYS).read_text()
    assert [ln for ln in keys.splitlines() if not ln.startswith("#")] == [
        f'command="{dp.entry_path(gen)}",{dp.KEY_OPTIONS} {PANEL_PUB}']
    assert fake.at(dp.SUDOERS).read_text() == dp.sudo_rule(USER, dp.dispatcher_path(gen),
                                                           cfg["passthrough"])
    assert fake.at(dp.ACCEPTENV).read_text() == dp.acceptenv_rule(USER, cfg["passthrough"])
    assert not fake.at(dp.SUDOERS_NEXT).exists()
    assert not fake.at(dp.ACCEPTENV_NEXT).exists()
    assert os.readlink(fake.at(LINK)) == dp.dispatcher_path(gen)
    assert sorted(p.name for p in fake.at(dp.GENERATIONS).iterdir()) == [gen]
    for name, text in (("entry", cfg["entry"]), ("dispatcher", cfg["dispatcher"])):
        f = fake.at(f"{dp.GENERATIONS}/{gen}/{name}")
        assert f.read_text() == dp._text(text)
        assert f.stat().st_mode & 0o777 == 0o755
    assert not list(fake.at(dp.PROOF_KEY_PARENT).iterdir()), "the proof key outlived its run"


# --- the generation ------------------------------------------------------------

def test_a_generation_is_named_by_every_part_of_the_path():
    base = dp.generation(config())
    assert re.fullmatch(r"[0-9a-f]{12}", base)
    assert dp.generation(config()) == base
    for change in (dict(entry=ENTRY + "# changed\n"),
                   dict(dispatcher=DISPATCHER + "# changed\n"),
                   dict(passthrough=NAMES + ["CATENA_NEW"]),
                   dict(passthrough=NAMES[:-1]),
                   dict(runner_user="another")):
        assert dp.generation(config(**change)) != base, change


def test_the_rules_carry_every_name_on_both_hops():
    sudo = dp.sudo_rule(USER, "/g/dispatcher", NAMES)
    assert f'Defaults:{USER} env_keep += "{" ".join(NAMES)}"' in sudo
    assert f"{USER} ALL=(root) NOPASSWD: /g/dispatcher" in sudo
    assert f"Defaults:{USER} !requiretty" in sudo
    accept = dp.acceptenv_rule(USER, NAMES)
    assert accept.endswith(f"Match User {USER}\n    AcceptEnv {' '.join(NAMES)}\nMatch all\n")


def test_the_running_generation_is_the_one_the_panel_keys_line_names():
    gen = "0123456789ab"
    assert dp.running_generation(f'command="{dp.entry_path(gen)}",no-pty {PANEL_PUB}') == gen
    assert dp.running_generation(LEGACY_LINE) == dp.LEGACY
    assert dp.running_generation(None) == dp.NONE
    assert dp.line_for(f"# {PANEL_PUB}\n{LEGACY_LINE}\n", "PANELBLOB") == LEGACY_LINE


# --- moving ------------------------------------------------------------------------

def test_a_first_install_ends_on_this_trees_generation(host):
    result = dp.switch(config(), host)
    assert result["ok"] and result["changed"], result
    assert_on(host, config())
    assert "proven" in result["summary"]
    kinds = [s["kind"] for s in host.fake.seen]
    assert kinds == ["proof", "proof", "panel", "panel", "panel", "panel"]


def test_a_host_on_the_installs_path_moves_and_keeps_it_until_the_proof(host):
    legacy(host)
    before = tree(host.root)
    result = dp.switch(config(), host)
    assert result["ok"], result
    assert_on(host, config())

    beside = host.fake.seen[0]
    assert beside["kind"] == "proof"
    assert LEGACY_LINE in beside["keys"].splitlines(), "the panel's line moved before the proof"
    proof_line = [ln for ln in beside["keys"].splitlines() if "PROOFBLOB" in ln]
    assert proof_line == [f'from="127.0.0.1,::1",expiry-time="{PROOF_EXPIRY}",'
                          f'command="{dp.entry_path(result["generation"])}",'
                          f'{dp.KEY_OPTIONS} ssh-ed25519 PROOFBLOB']
    assert beside["keys"].startswith(before[KEYS].decode()), "the panel's file was rewritten"
    assert {"catena-admin-runner", "catena-admin-runner-next"} <= set(beside["sudoers"])
    assert set(NAMES) <= beside["loaded"], "sshd was not reloaded before the proof"


def test_a_host_on_an_older_generation_moves_and_the_older_one_goes(host):
    old = config(passthrough=LEGACY_NAMES)
    older = on_generation(host, old)
    result = dp.switch(config(), fresh_host(host))
    assert result["ok"], result
    assert result["generation"] != older
    assert_on(host, config())
    assert f"generation {older}" in result["summary"]


def test_a_running_generation_changes_nothing(host):
    on_generation(host, config())
    before = tree(host.root)
    again = fresh_host(host)
    result = dp.switch(config(), again)
    assert result == {"ok": True, "changed": False, "generation": dp.generation(config()),
                      "wrote": [], "summary": result["summary"]}
    assert tree(host.root) == before
    assert host.fake.calls == [], "a run with nothing to move dispatched or reloaded"


# --- the fallback ----------------------------------------------------------------

def test_a_failed_proof_before_the_switch_leaves_the_running_path_as_it_was(host):
    legacy(host)
    before = tree(host.root)
    host.fake.interfere = lambda fake, kind, request: (
        _done(1, stderr="sudo: a password is required") if kind == "proof" else None)
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "the path an install wrote" in result["summary"]
    assert "proof through generation" in result["summary"]
    assert tree(host.root) == before
    assert not host.fake.at(dp.GENERATIONS).exists() or not list(
        host.fake.at(dp.GENERATIONS).iterdir())
    assert not list(host.fake.at(dp.PROOF_KEY_PARENT).iterdir())
    assert host.fake.calls[-1] == [dp.SYSTEMCTL, "reload", "ssh"], (
        "sshd kept the removed -next rule loaded")
    assert not any(s["kind"] == "panel" for s in host.fake.seen), "the panel's key moved"


def test_a_failed_proof_after_the_switch_puts_the_previous_line_back(host):
    legacy(host)
    before = tree(host.root)
    host.fake.interfere = lambda fake, kind, request: (
        _done(1, stderr="the switched line fails") if kind == "panel" else None)
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "after the switch" in result["summary"]
    assert tree(host.root) == before


def test_a_failed_proof_after_retiring_puts_the_next_rules_back(host):
    def fail_once_the_next_rules_are_gone(fake, kind, request):
        if kind == "panel" and not fake.at(dp.SUDOERS_NEXT).exists():
            return _done(1, stderr="lost on retiring")
        return None

    legacy(host)
    host.fake.interfere = fail_once_the_next_rules_are_gone
    result = dp.switch(config(), host)
    gen = dp.generation(config())
    assert not result["ok"]
    assert f"the panel dispatches through generation {gen}" in result["summary"]
    assert "-next rules are back" in result["summary"]
    fake = host.fake
    assert fake.at(dp.SUDOERS_NEXT).read_text() == dp.sudo_rule(
        USER, dp.dispatcher_path(gen), NAMES)
    assert fake.at(dp.ACCEPTENV_NEXT).read_text() == dp.acceptenv_rule(USER, NAMES)
    assert dp.entry_path(gen) in fake.at(KEYS).read_text()
    assert set(NAMES) <= fake.loaded


def test_a_drop_in_sshd_refuses_never_reaches_a_reload(host):
    legacy(host)
    before = tree(host.root)
    host.fake.rc["sshd"] = 1
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "sshd refused" in result["summary"]
    assert tree(host.root) == before
    assert [dp.SYSTEMCTL, "reload", "ssh"] not in host.fake.calls
    assert not host.fake.seen


def test_a_sudo_rule_visudo_refuses_never_lands(host):
    legacy(host)
    before = tree(host.root)
    host.fake.rc["visudo"] = 1
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "visudo refused" in result["summary"]
    assert tree(host.root) == before
    assert not any(p.name.endswith(".tmp") for p in host.fake.at("/etc/sudoers.d").iterdir())


def test_a_run_cut_short_after_the_switch_finishes_retiring(host):
    """The panel's line is on the new generation while its rules are still in
    the -next files and the install's are in the main ones: the next run moves
    them, proves the result, and only then removes the install's path."""
    legacy(host)
    host.fake.interfere = lambda fake, kind, request: (
        _done(1) if kind == "panel" and not fake.at(dp.SUDOERS_NEXT).exists() else None)
    assert not dp.switch(config(), host)["ok"]
    for path, text in ((dp.SUDOERS, LEGACY_SUDOERS), (dp.ACCEPTENV, LEGACY_ACCEPTENV)):
        host.fake.at(path).unlink()
        host.fake.at(path).write_text(text)
    host.fake.interfere = None
    host.fake.seen.clear()
    result = dp.switch(config(), fresh_host(host))
    assert result["ok"], result
    assert_on(host, config())
    assert [s["kind"] for s in host.fake.seen] == ["panel", "panel"]


def test_a_stale_proof_line_is_dropped_and_the_result_proven(host):
    gen = on_generation(host, config())
    fake = host.fake
    fake.at(KEYS).write_text(fake.at(KEYS).read_text()
                             + f'from="127.0.0.1,::1",command="{dp.entry_path(gen)}",'
                               f'{dp.KEY_OPTIONS} ssh-ed25519 STALEBLOB\n')
    stale = fake.at(dp.PROOF_KEY_PARENT) / "catena-dispatch-proof.killed"
    stale.mkdir()
    result = dp.switch(config(), fresh_host(host))
    assert result["ok"] and result["changed"], result
    assert result["wrote"] == [KEYS]
    assert_on(host, config())
    assert [s["kind"] for s in fake.seen] == ["panel", "panel"]


# --- the proof ---------------------------------------------------------------------

def test_a_variable_one_hop_drops_fails_the_proof(host):
    """sshd accepting a variable sudo then drops is the failure the two hops
    hide: the action runs and reads an empty string."""
    legacy(host)
    before = tree(host.root)
    real_sudo = Fake.sudo

    def sudo_dropping_the_token(self):
        allowed, keep = real_sudo(self)
        return allowed, keep - {"CATENA_CF_CANDIDATE_TOKEN"}

    host.fake.sudo = sudo_dropping_the_token.__get__(host.fake)
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "CATENA_CF_CANDIDATE_TOKEN did not reach the dispatcher" in result["summary"]
    assert tree(host.root) == before


def test_a_generation_whose_entry_lets_a_payload_through_is_refused(host):
    legacy(host)
    host.fake.interfere = lambda fake, kind, request: (
        _done(1, stderr="unknown action") if " " in request else None)
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "not refused by the entry before sudo" in result["summary"]


def test_another_generation_answering_fails_the_proof(host):
    legacy(host)
    host.fake.interfere = lambda fake, kind, request: (
        _done(0, stdout="dispatch-path-proof 000000000000\n") if " " not in request else None)
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "did not answer the proof" in result["summary"]


def test_transport_failures_are_retried_and_nothing_else_is(host):
    host.fake.transport_failures = 2
    result = dp.switch(config(), host)
    assert result["ok"], result
    assert host.sleeps == [dp.DELAY_S, dp.DELAY_S]

    other = fresh_host(host)
    on = dp.generation(config())
    host.fake.calls.clear()
    host.fake.interfere = lambda fake, kind, request: _done(1, stderr="refused")
    result = dp.switch(config(passthrough=NAMES[:-1]), other)
    assert not result["ok"]
    ssh_calls = [c for c in host.fake.calls if c[0] == dp.SSH]
    assert len(ssh_calls) == 1, "a refusal was retried as if it were a transport failure"
    assert f"generation {on}" in result["summary"]


def test_the_proof_dispatches_as_the_panel_does(host):
    argv = dp.ssh_argv(config(), "/k", dp.PROOF_ACTION, {"A": "1", "B": "1"})
    assert argv[:3] == [dp.SSH, "-F", "/dev/null"]
    joined = " ".join(argv)
    for option in ("StrictHostKeyChecking=yes", f"UserKnownHostsFile={KEYDIR}/known_hosts",
                   "HostKeyAlias=host.docker.internal", "HostKeyAlgorithms=ssh-ed25519",
                   "BatchMode=yes", "IdentitiesOnly=yes", "SetEnv A=1 B=1"):
        assert option in joined, option
    assert argv[-2:] == [f"{USER}@127.0.0.1", dp.PROOF_ACTION]


# --- the runner owns its ~/.ssh ----------------------------------------------------

def ssh_dir(host) -> Path:
    return host.fake.at(f"{HOME}/.ssh")


def test_a_link_planted_as_the_temporary_is_replaced_not_written_through(host):
    legacy(host)
    victim = host.fake.at("/usr/local/lib/victim")
    victim.parent.mkdir(parents=True)
    victim.write_text("SAFE")
    victim.chmod(0o640)
    (ssh_dir(host) / "authorized_keys.tmp").symlink_to(victim)
    result = dp.switch(config(), host)
    assert result["ok"], result
    assert_on(host, config())
    assert victim.read_text() == "SAFE" and victim.stat().st_mode & 0o777 == 0o640
    assert not (ssh_dir(host) / "authorized_keys.tmp").exists()


def test_a_link_planted_as_authorized_keys_is_refused_unread(host):
    legacy(host)
    secret = host.fake.at("/etc/secret")
    secret.write_text("ROOT ONLY")
    keys = ssh_dir(host) / "authorized_keys"
    keys.unlink()
    keys.symlink_to(secret)
    result = dp.switch(config(), host)
    assert not result["ok"] and "not a plain file" in result["summary"], result
    assert secret.read_text() == "ROOT ONLY"
    assert keys.is_symlink()
    assert host.fake.calls == []


def test_a_symlinked_ssh_directory_is_refused(host):
    elsewhere = host.fake.at("/etc/cron.daily")
    elsewhere.mkdir()
    shutil.rmtree(ssh_dir(host))
    ssh_dir(host).symlink_to(elsewhere)
    result = dp.switch(config(), host)
    assert not result["ok"] and "not a plain directory" in result["summary"], result
    assert list(elsewhere.iterdir()) == []
    assert host.fake.calls == []


def test_an_ssh_directory_the_runner_does_not_own_is_refused(host):
    other = dp.Host(str(host.root), run=host.fake, ids=ids_of([], uid_shift=1),
                    sleep=host.sleeps.append, now=lambda: NOW)
    result = dp.switch(config(), other)
    assert not result["ok"] and f"not owned by {USER}" in result["summary"], result
    assert host.fake.calls == []


def test_a_missing_ssh_directory_is_refused(host):
    shutil.rmtree(ssh_dir(host))
    result = dp.switch(config(), host)
    assert not result["ok"] and "is missing" in result["summary"], result


# --- what the panel's key and the key options allow ------------------------------

@pytest.mark.parametrize("pub", [
    "ssh-ed25519 PANELBLOB\nssh-ed25519 EVILBLOB",
    "ssh-ed25519 PANELBLOB\n\ncommand=\"x\" ssh-rsa AAAA",
    "ssh-rsa AAAAB3NzaC1yc2E",
    "ecdsa-sha2-nistp256 AAAAE2VjZHNh",
    "no-pty ssh-ed25519 PANELBLOB",
    'ssh-ed25519 PANELBLOB" extra',
    "ssh-ed25519 PANELBLOB two words",
])
def test_a_panel_key_that_is_not_one_ed25519_line_is_refused(host, pub):
    legacy(host)
    host.fake.at(f"{KEYDIR}/id_ed25519.pub").write_text(pub + "\n")
    before = tree(host.root)
    result = dp.switch(config(), host)
    assert not result["ok"] and "not exactly one ssh-ed25519 line" in result["summary"], result
    assert tree(host.root) == before
    assert host.fake.calls == []


def test_a_panel_key_with_a_comment_is_one_line(host):
    host.fake.at(f"{KEYDIR}/id_ed25519.pub").write_text("ssh-ed25519 PANELBLOB panel@host\n")
    result = dp.switch(config(), host)
    assert result["ok"], result
    lines = [ln for ln in host.fake.at(KEYS).read_text().splitlines() if not ln.startswith("#")]
    assert len(lines) == 1 and lines[0].endswith(" ssh-ed25519 PANELBLOB panel@host")


def test_key_lines_are_restricted_and_a_proof_line_expires():
    assert dp.KEY_OPTIONS == "restrict"
    line = dp.key_line("/e", "ssh-ed25519 K", loopback_only=True, expires=PROOF_EXPIRY)
    assert line == (f'from="127.0.0.1,::1",expiry-time="{PROOF_EXPIRY}",command="/e",restrict '
                    "ssh-ed25519 K")
    assert dp.key_line("/e", "ssh-ed25519 K") == 'command="/e",restrict ssh-ed25519 K'


@pytest.mark.parametrize("name", [
    "lower", "A B", "FOO*", "*", "", "1A", "A-B", "FOO\n", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "BASH_ENV", "BASH_FUNC_x", "ENV", "PATH", "IFS", "PYTHONPATH", "PYTHONSTARTUP", "PERL5LIB",
    "PERL5OPT", "GCONV_PATH", "HOME", "SHELL", 7, "SHELLOPTS", "BASHOPTS", "TERM", "CATENA",
    "CATENA_", "X_FORWARDED_", "CATENA_ADMIN_ACTIONS_FILE",
])
def test_a_passthrough_name_that_is_not_a_plain_variable_is_refused(host, name):
    legacy(host)
    before = tree(host.root)
    result = dp.switch(config(passthrough=NAMES + [name]), host)
    assert not result["ok"] and "passthrough name" in result["summary"], result
    assert tree(host.root) == before
    assert host.fake.calls == []


def test_the_shipped_passthrough_names_are_accepted():
    shipped = ["X_FORWARDED_EMAIL", "CATENA_DISPATCH_PAYLOAD", "CATENA_CF_CANDIDATE_TOKEN",
               "CATENA_BACKUP_CANDIDATE_REPO", "CATENA_BACKUP_CANDIDATE_ACCESS_KEY",
               "CATENA_BACKUP_CANDIDATE_SECRET_KEY"]
    assert dp.passthrough_problem(shipped) == ""
    assert dp.passthrough_problem("A B") != ""


# --- a running generation is trusted only when it is whole ------------------------

def tamper_with_the_dispatcher(host, gen: str) -> None:
    host.fake.at(dp.dispatcher_path(gen)).write_text("#!/bin/sh\nexit 0\n")


@pytest.mark.parametrize("damage", ["altered", "missing file", "missing directory", "mode",
                                    "extra file", "link"])
def test_a_damaged_running_generation_is_reinstalled_and_proven(host, damage):
    gen = on_generation(host, config())
    root = host.fake.at(f"{dp.GENERATIONS}/{gen}")
    if damage == "altered":
        tamper_with_the_dispatcher(host, gen)
    elif damage == "missing file":
        (root / "dispatcher").unlink()
    elif damage == "missing directory":
        shutil.rmtree(root)
    elif damage == "mode":
        (root / "entry").chmod(0o777)
    elif damage == "extra file":
        (root / "extra").write_text("x")
    else:
        # A link to a copy with the same content and mode: only the no-links
        # rule tells it from the real file.
        copy = host.fake.at("/usr/local/lib/entry-copy")
        copy.write_text((root / "entry").read_text())
        copy.chmod(0o755)
        (root / "entry").unlink()
        (root / "entry").symlink_to(copy)
    result = dp.switch(config(), fresh_host(host))
    assert result["ok"] and result["changed"], result
    assert_on(host, config())
    assert [s["kind"] for s in host.fake.seen] == ["panel", "panel"], "the repair was not proven"


def test_a_running_generation_not_owned_by_root_is_reinstalled(host):
    """Only the root-owner check can fail here: content, mode and kind are right."""
    gen = on_generation(host, config())

    def ids(user: str) -> tuple[int, int]:
        return (os.getuid() + (1 if user == "root" else 0)), os.getgid()

    other = dp.Host(str(host.root), run=host.fake, ids=ids, sleep=host.sleeps.append,
                    now=lambda: NOW)
    files = {"entry": dp._text(ENTRY), "dispatcher": dp._text(DISPATCHER)}
    assert fresh_host(host).generation_intact(gen, files)
    assert not other.generation_intact(gen, files)
    result = dp.switch(config(), other)
    assert result["ok"] and result["changed"], result
    assert f"{dp.GENERATIONS}/{gen}/entry" in result["wrote"], "the generation was trusted"
    assert_on(host, config())


def test_a_damaged_running_generation_that_does_not_prove_keeps_the_other_generations(host):
    gen = on_generation(host, config())
    other = host.fake.at(f"{dp.GENERATIONS}/0123456789ab")
    other.mkdir()
    (other / "dispatcher").write_text("#!/bin/sh\n")
    tamper_with_the_dispatcher(host, gen)
    host.fake.interfere = lambda fake, kind, request: (
        _done(1, stderr="the repaired path fails") if kind == "panel" else None)
    result = dp.switch(config(), fresh_host(host))
    assert not result["ok"] and "did not prove itself" in result["summary"], result
    assert (other / "dispatcher").exists(), "a generation went before the running one proved"


# --- what it holds and reports ---------------------------------------------------

def test_the_runners_keys_hold_the_panels_line_alone_owned_by_the_runner(host):
    legacy(host)
    result = dp.switch(config(), host)
    assert result["ok"], result
    keys = host.fake.at(KEYS)
    assert keys.stat().st_mode & 0o777 == 0o600
    assert "PROOFBLOB" not in keys.read_text()


def test_every_write_of_the_runners_keys_is_chowned_to_the_runner(host, monkeypatch):
    legacy(host)
    chowned: list[tuple[int, int]] = []
    monkeypatch.setattr(dp.os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)))
    gid = os.getgid() + 4321
    other = dp.Host(str(host.root), run=host.fake, ids=lambda user: (os.getuid(), gid),
                    sleep=host.sleeps.append, now=lambda: NOW)
    result = dp.switch(config(), other)
    assert result["ok"], result
    assert len(chowned) >= 3, "the beside, put-back and switch writes were not all chowned"
    assert set(chowned) == {(os.getuid(), gid)}


def _keys_with_file_owner(host, monkeypatch, owner: int, root_uid: int) -> dp.RunnerKeys:
    """The runner's keys as seen when the file reports `owner` and root is `root_uid`."""
    real = os.fstat

    def fstat(fd):
        st = real(fd)
        if not stat.S_ISREG(st.st_mode):
            return st
        fields = list(st)
        fields[4] = owner
        return os.stat_result(fields)

    monkeypatch.setattr(dp.os, "fstat", fstat)
    h = dp.Host(str(host.root), run=host.fake,
                ids=lambda user: (root_uid if user == "root" else os.getuid(), os.getgid()),
                sleep=host.sleeps.append, now=lambda: NOW)
    return dp.RunnerKeys(h, HOME, USER)


def test_authorized_keys_with_a_second_name_is_refused(host):
    legacy(host)
    os.link(ssh_dir(host) / "authorized_keys", ssh_dir(host) / "second-name")
    result = dp.switch(config(), host)
    assert not result["ok"] and "links" in result["summary"], result
    assert host.fake.calls == []


def test_authorized_keys_owned_by_neither_the_runner_nor_root_is_refused(host, monkeypatch):
    legacy(host)
    keys = _keys_with_file_owner(host, monkeypatch, owner=os.getuid() + 7, root_uid=os.getuid() + 9)
    with pytest.raises(dp.Refused, match="neither"):
        keys.read()
    keys.close()


def test_authorized_keys_owned_by_the_runner_or_root_is_read(host, monkeypatch):
    legacy(host)
    runner = _keys_with_file_owner(host, monkeypatch, owner=os.getuid(), root_uid=os.getuid() + 9)
    assert runner.read()
    runner.close()
    root = _keys_with_file_owner(host, monkeypatch, owner=os.getuid() + 9, root_uid=os.getuid() + 9)
    assert root.read()
    root.close()


def test_no_install_task_follows_a_link_on_a_path_a_less_privileged_account_owns():
    """The runner owns its home and the panel's uid owns its SSH and state
    directories. A link planted there makes the module fail on the link;
    following it would chown the link's target."""
    owned = ("catena_admin_runner_home", "catena_admin_host_ssh_dir", "catena_admin_state_dir")
    checked = []
    for task in walk_tasks(yaml.safe_load(PANEL_HOST_TASKS.read_text())):
        for module in ("ansible.builtin.file", "ansible.builtin.copy", "ansible.builtin.template"):
            args = task.get(module)
            if not isinstance(args, dict):
                continue
            target = str((args or {}).get("path") or (args or {}).get("dest") or "")
            target += str(task.get("loop") or "")
            if any(name in target for name in owned):
                checked.append(task["name"])
                assert args.get("follow") is False, task["name"]
    assert len(checked) == 6, checked


def test_the_verdict_names_every_path_written_and_no_key(host):
    legacy(host)
    result = dp.switch(config(), host)
    gen = result["generation"]
    assert result["ok"], result
    assert {KEYS, dp.SUDOERS, dp.SUDOERS_NEXT, dp.ACCEPTENV, dp.ACCEPTENV_NEXT, LINK,
            f"{dp.GENERATIONS}/{gen}/entry", f"{dp.GENERATIONS}/{gen}/dispatcher"} == set(
        result["wrote"])
    assert not any(p.startswith(dp.PROOF_KEY_PARENT) or p.startswith(KEYDIR)
                   for p in result["wrote"])


def test_a_missing_panel_key_writes_nothing(host):
    host.fake.at(f"{KEYDIR}/id_ed25519.pub").unlink()
    before = tree(host.root)
    result = dp.switch(config(), host)
    assert not result["ok"]
    assert "id_ed25519.pub is missing" in result["summary"]
    assert tree(host.root) == before
    assert host.fake.calls == []


def test_unreadable_input_is_a_verdict_not_a_crash(monkeypatch, capsys):
    import io
    import json
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert dp.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["wrote"] == []
