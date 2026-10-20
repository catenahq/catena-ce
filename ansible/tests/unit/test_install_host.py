"""install-host.sh: the install, run on the server from the release's own tree.

What it refuses before touching anything, and the order of the chain, which
is where the lockout rule lives: the accounts leg closes nothing, and the
system leg proves its session before the first playbook hardens sshd.

Run: uv run pytest tests/unit/test_install_host.py
"""
from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE / "install-host.sh"
STARTER = ANSIBLE.parent / "installer" / "catena_installer" / "starter.sh"

FULL = ["--leg", "system", "--image", "ghcr.io/catenahq/catena-admin:v1.2.3@sha256:" + "0" * 64,
        "--name", "prod1", "--ops-user", "ops", "--ops-key", "/tmp/ops.pub",
        "--env-file", "/tmp/env"]


def _run(*args: str, script: Path = SCRIPT) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True)


@pytest.mark.parametrize("args, said", [
    (["--leg", "nonsense"], "--leg must be accounts, system or uninstall"),
    (["--leg", "system", "--bogus", "x"], "unknown argument: --bogus"),
    (FULL[:2], "--image is required"),
    ([a for a in FULL if a not in ("--ops-key", "/tmp/ops.pub")], "--ops-key is required"),
])
def test_a_wrong_call_is_refused_before_anything_runs(args, said):
    done = _run(*args)
    assert done.returncode == 2
    assert said in done.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as root")
def test_it_runs_as_root_only():
    done = _run(*FULL)
    assert done.returncode == 2 and "run as root" in done.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as root")
def test_the_starter_runs_as_root_only():
    done = _run("--", *FULL, script=STARTER)
    assert done.returncode == 2 and "run as root" in done.stderr


PROOF = 'python3 "$ansible_dir/helpers/session_proof.py"'


def _at(text: str, needle: str) -> int:
    assert needle in text, needle
    return text.index(needle)


def test_the_chain_runs_in_order_and_proves_the_session_first():
    text = SCRIPT.read_text()
    accounts = _at(text, "play accounts.yml")
    proof = _at(text, PROOF)
    bootstrap = _at(text, "play bootstrap.yml")
    keyset = _at(text, "play show-keyset.yml")
    converge = _at(text, "play converge.yml")
    pin = _at(text, "catena-stack-update-managed pin --image")
    validate = _at(text, "play validate.yml")
    assert accounts < proof < bootstrap < keyset < converge < pin < validate


def test_the_accounts_leg_stops_before_the_proof():
    """It runs through the provider's login: nothing after the accounts may
    run on that leg."""
    text = SCRIPT.read_text()
    leg = text[_at(text, 'if [ "$leg" = accounts ]'):_at(text, PROOF)]
    assert "exit 0" in leg


def test_a_scoped_converge_records_no_version():
    """--tags applies part of a release, which is not one to record."""
    text = SCRIPT.read_text()
    guard = text[_at(text, 'if [ -z "$tags" ]'):]
    assert guard.index("catena-stack-update-managed pin") < guard.index("fi")


HOST_LOCK = "/run/catena.lock"


def _converge_on(lock: Path) -> subprocess.Popen:
    """The script's converge step, from its tags to its lock, run against
    `lock` with a play that reports whether the lock is held while it runs."""
    text = SCRIPT.read_text()
    tail = f") 9>>{HOST_LOCK} || exit 1\n"
    step = text[_at(text, "converge=()"):_at(text, tail) + len(tail)]
    assert "flock -w 3600 9" in step, "the hour of wait catena-converge runs with"
    harness = (
        "set -euo pipefail\n"
        "say() { printf 'catena-install: %s\\n' \"$*\" >&2; }\n"
        f"play() {{ flock -n {lock} true && echo free || echo held; echo \"play $*\"; }}\n"
        'extra=(-e @vars.json) tags=""\n'
        + step.replace(HOST_LOCK, str(lock))
        + f"flock -n {lock} true && echo released\n")
    return subprocess.Popen(["timeout", "30", "bash", "-c", harness],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_the_converge_waits_for_the_host_lock_and_holds_it(tmp_path):
    """A lane holding /run/catena.lock (catena-admin's catena-converge, the
    engine reinstall, the nightly lanes) holds the install's converge back, and
    the play runs holding the lock, so nothing refills the payload stage
    beside it. The lock is free again once the play ends."""
    lock = tmp_path / "catena.lock"
    with lock.open("w") as lane:
        fcntl.flock(lane, fcntl.LOCK_EX)
        run = _converge_on(lock)
        assert "waiting for another lane holding" in run.stderr.readline()
    out, err = run.communicate(timeout=30)
    assert run.returncode == 0, err
    assert out.splitlines() == ["held", "play converge.yml -e @vars.json", "released"]


def test_the_image_reaches_the_converge_for_the_panel_and_the_engines():
    """One variable: reconcile/roles/payload and reconcile/roles/catena-admin
    both read catena_admin_image."""
    text = SCRIPT.read_text()
    assert 'export CATENA_ADMIN_IMAGE="$image"\n' in text
    assert "CATENA_PAYLOAD_IMAGE=" not in text


def test_the_starter_holds_nothing_version_specific():
    """It must start the install of any release: it fetches and hands over
    to the fetched tree's own install-host.sh."""
    text = STARTER.read_text()
    assert "fetch_release.py" in text
    assert 'usr/local/share/catena-ce/ansible/install-host.sh' in text
    assert "ansible-playbook" not in text and "playbooks/" not in text


LOGS = "/var/lib/catena/install/logs"
BLOCK = '{"admin_password": "x"}'


def _leg(tmp_path: Path, body: str, *, wait: str = "sleep 20") -> list[str]:
    """The script's log redirect, passwords print and exit trap, with `logs`
    under tmp_path, followed by `body`, as a bash command line."""
    text = SCRIPT.read_text()
    setup = text[_at(text, "logs=" + LOGS):_at(text, "\nstart_log\n") + len("\nstart_log\n")]
    trap = next(line for line in text.splitlines() if line.startswith("trap 'rc=$?"))
    func = text[_at(text, "show_keyset() {"):]
    func = func[:func.index("\n}\n") + 3]
    (tmp_path / "work").mkdir()
    harness = (
        "set -euo pipefail\n"
        f'KEYSET_BEGIN="-----BEGIN CATENA PASSWORDS-----" KEYSET_END="-----END CATENA PASSWORDS-----"\n'
        "say() { printf 'catena-install: %s\\n' \"$*\" >&2; }\n"
        f"leg=system work={tmp_path / 'work'}\n"
        + setup.replace(LOGS, str(tmp_path / "logs")).replace("sleep 20", wait)
        + trap + "\n"
        + f"keyset='{BLOCK}'\n" + func + "\n" + body + "\n")
    return ["timeout", "60", "bash", "-c", harness]


def _block(out: str) -> list[str]:
    lines = out.splitlines()
    begin = lines.index("-----BEGIN CATENA PASSWORDS-----")
    return lines[begin:begin + 3]


def test_a_closed_session_leaves_the_leg_running_to_its_end(tmp_path):
    """The leg's output is copied to its log on the server; the session
    closing stops the print to the session and nothing else."""
    body = ('for i in $(seq 1 20); do echo "line $i"; sleep 0.1; done\n'
            "show_keyset\nsay done\nexit 7")
    run = subprocess.Popen(_leg(tmp_path, body), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert run.stdout.readline() == "line 1\n"
    run.stdout.close()
    assert run.wait(timeout=10) == 7
    (log,) = (tmp_path / "logs").iterdir()
    lines = log.read_text().splitlines()
    assert [f"line {i}" for i in range(1, 21)] == [l for l in lines if l.startswith("line ")]
    assert "catena-install: done" in lines
    assert not (tmp_path / "work").exists()


def test_the_passwords_block_reaches_the_session_and_stays_out_of_the_log(tmp_path):
    done = subprocess.run(_leg(tmp_path, "echo before\nshow_keyset\necho after"),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    assert _block(done.stdout) == ["-----BEGIN CATENA PASSWORDS-----", BLOCK,
                                   "-----END CATENA PASSWORDS-----"]
    (log,) = (tmp_path / "logs").iterdir()
    text = log.read_text()
    assert "before" in text and "after" in text
    assert "CATENA PASSWORDS" not in text and "admin_password" not in text


def test_the_passwords_block_is_never_split_by_an_earlier_line(tmp_path):
    """tee forwards the line before the block late when nothing waits for it."""
    body = ('for i in $(seq 1 30); do echo "ansible line $i"; done\n'
            "say 'passwords, as shown after bootstrap:'\nshow_keyset")
    for n in range(200):
        run_dir = tmp_path / f"r{n}"
        run_dir.mkdir()
        done = subprocess.run(_leg(run_dir, body), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
        assert _block(done.stdout) == ["-----BEGIN CATENA PASSWORDS-----", BLOCK,
                                       "-----END CATENA PASSWORDS-----"], (n, done.stdout)


def test_the_logs_are_private_and_only_the_newest_ten_stay(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o755)
    for day in range(1, 13):
        (logs / f"system-202001{day:02d}T000000Z.log").write_text("old")
    (logs / "accounts-20200101T000000Z.log").write_text("other leg")
    done = subprocess.run(_leg(tmp_path, "echo hi"), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    system = sorted(p for p in logs.iterdir() if p.name.startswith("system-"))
    assert len(system) == 10 and system[0].name == "system-20200104T000000Z.log"
    assert (logs / "accounts-20200101T000000Z.log").exists()
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    assert stat.S_IMODE(system[-1].stat().st_mode) == 0o600 and system[-1].read_text() == "hi\n"


def test_a_child_holding_the_pipe_does_not_hold_the_exit_forever(tmp_path):
    body = "( sleep 30 ) 3>&- &\necho hi\nexit 5"
    start = time.monotonic()
    done = subprocess.run(_leg(tmp_path, body, wait="sleep 1"), text=True, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert done.returncode == 5
    assert time.monotonic() - start < 15


def test_the_install_lock_is_free_as_soon_as_the_leg_exits(tmp_path):
    """The leg is started holding the starter's lock on fd 8; nothing it starts
    for its log keeps that lock after it ends."""
    lock = tmp_path / "install.lock"
    holder = 'exec 8>>"$0"; flock -n 8 && exec "$@"'
    done = subprocess.run(["bash", "-c", holder, str(lock), *_leg(tmp_path, "echo hi\nexit 3")],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True)
    assert done.returncode == 3
    with lock.open("w") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _starter_lock(lock: Path) -> list[str]:
    """The starter's install lock, then a child that reports it is running."""
    text = STARTER.read_text()
    block = text[_at(text, "exec 8>>/run/catena-install.lock"):_at(text, "\nmissing=()")]
    harness = ("set -euo pipefail\n"
               "say() { printf 'catena-install: %s\\n' \"$*\" >&2; }\n"
               + block.replace("/run/catena-install.lock", str(lock))
               + "exec bash -c 'echo ready; sleep 3'\n")
    return ["timeout", "30", "bash", "-c", harness]


def test_the_starter_waits_for_an_earlier_install_and_hands_its_lock_on(tmp_path):
    """An abandoned leg holds /run/catena-install.lock: a rerun waits for it
    before fetching, and holds it through the exec into install-host.sh."""
    lock = tmp_path / "install.lock"
    with lock.open("w") as earlier:
        fcntl.flock(earlier, fcntl.LOCK_EX)
        run = subprocess.Popen(_starter_lock(lock), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        assert "waiting for an earlier install" in run.stderr.readline()
    assert run.stdout.readline() == "ready\n"
    with lock.open("w") as probe, pytest.raises(BlockingIOError):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run.communicate(timeout=30)
