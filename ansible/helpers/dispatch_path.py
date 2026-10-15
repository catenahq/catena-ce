#!/usr/bin/env python3
"""Move the panel's dispatch path to this tree's generation: beside the running one, proven, then switched.

Runs ON the host as root, shipped by reconcile/roles/dispatch_path. The path is
what an SSH request from the panel crosses to become a root action: sshd's
AcceptEnv for the runner account, the forced command on the panel key's
authorized_keys line, the runner's sudo rule and its env_keep, and the
dispatcher.

A GENERATION is one rendering of that path, named by the hash of what it is
made of: the entry (the forced command), the dispatcher, the passthrough names
and the rules and key line rendered from them. Its two executables live in
GENERATIONS/<generation>/. The generation the panel runs is the one the panel
key's command= names. A line with any other command runs a path an install
wrote (LEGACY), which a switch replaces as it replaces a generation.

When the running generation is not this tree's:

  1. BESIDE. The new generation's directory, its sudo rule (SUDOERS_NEXT) and
     its AcceptEnv rule (ACCEPTENV_NEXT) are added next to the running ones.
     sudo's env_keep and sshd's AcceptEnv add up across files, so nothing the
     running generation reads changes.
  2. PROOF. A key minted for this run, authorized from loopback for the new
     entry alone, dispatches the proof action through sshd, the entry, sudo
     and the dispatcher with every passthrough variable set: the new generation
     has to answer, every variable has to arrive, and a request carrying more
     than the action name has to be refused by the entry. The key and its line
     are removed whatever the result.
  3. SWITCH. The panel key's line is pointed at the new entry, and the panel's
     own key runs the same proof.

Then, with this tree's generation running:

  4. RETIRE. Its rules move into SUDOERS and ACCEPTENV, the -next files go, and
     the panel's key proves the result when anything changed. The dispatcher
     link follows and every other generation's directory is removed.

A failed proof before the switch removes what BESIDE added. A failed proof
after the switch puts the previous authorized_keys back. A failed proof after
retiring puts the -next files back, the state the SWITCH proved. Each is a
refusal that says what failed: the converge stops, and the panel keeps a path
it could dispatch through.

The panel's key, the runner account, the host key and the panel's known_hosts
are the install's (bootstrap/roles/catena_admin_host): this reads them and
writes none of them, and the runner's authorized_keys holds the panel's line
alone. The proof key exists for one run, in a root-only directory under /run.

Input: one JSON object on stdin: runner_user, runner_home, panel_key_dir, link
(the dispatcher's stable path), passthrough (the variable names), entry and
dispatcher (the two scripts' text). Output: one JSON object on stdout, {"ok",
"changed", "generation", "wrote", "summary"}, and exit 0 whatever the verdict;
the caller asserts on it. "wrote" is every path this run created, changed or
removed, which the converge's drift report leaves out.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

GENERATIONS = "/usr/local/lib/catena-dispatch"
SUDOERS = "/etc/sudoers.d/catena-admin-runner"
SUDOERS_NEXT = "/etc/sudoers.d/catena-admin-runner-next"
ACCEPTENV = "/etc/ssh/sshd_config.d/30-catena-admin-runner-acceptenv.conf"
ACCEPTENV_NEXT = "/etc/ssh/sshd_config.d/30-catena-admin-runner-next.conf"
# tmpfs, root's: the proof key never reaches a disk.
PROOF_KEY_PARENT = "/run"

PROOF_ACTION = "dispatch-path-proof"
ENTRY_REFUSAL = "catena-dispatch-entry: refusing"
# The name the panel reaches this host by, under which the panel's known_hosts
# lists the host key (bootstrap/roles/catena_admin_host's keyscan).
HOST_ALIAS = "host.docker.internal"
LOOPBACK = "127.0.0.1"
KEY_OPTIONS = "no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"

SSH = "/usr/bin/ssh"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
SSHD = "/usr/sbin/sshd"
SYSTEMCTL = "/usr/bin/systemctl"
VISUDO = "/usr/sbin/visudo"

# Exit 255 is ssh's own failure, which an sshd reload in progress also gives.
ATTEMPTS = 5
DELAY_S = 2.0

LEGACY = "legacy"
NONE = "none"

HEADER = "# Written by catena-ce reconcile/roles/dispatch_path (helpers/dispatch_path.py).\n"

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class Refused(Exception):
    """A step that failed, said in a sentence, after its own undo ran."""


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL, check=False)


def _chown(path: Path, user: str) -> None:
    shutil.chown(path, user, user)


def _tail(text: str | None) -> str:
    return " ".join((text or "").split())[-300:] or "no output"


def _text(script: str) -> str:
    return script.rstrip("\n") + "\n"


# --- what a generation is made of -------------------------------------------

def sudo_rule(user: str, dispatcher: str, names: list[str]) -> str:
    return (HEADER
            + "# The runner account may run this generation's dispatcher as root, with no\n"
            + "# password and no tty, keeping the dispatch variables sshd accepted for it.\n"
            + f"Defaults:{user} !requiretty\n"
            + f'Defaults:{user} env_keep += "{" ".join(names)}"\n'
            + f"{user} ALL=(root) NOPASSWD: {dispatcher}\n")


def acceptenv_rule(user: str, names: list[str]) -> str:
    return (HEADER
            + "# The dispatch variables the panel sends, accepted for the runner account alone.\n"
            + f"Match User {user}\n"
            + f"    AcceptEnv {' '.join(names)}\n"
            + "Match all\n")


def key_line(entry: str, pubkey: str, *, loopback_only: bool = False) -> str:
    source = f'from="{LOOPBACK},::1",' if loopback_only else ""
    return f'{source}command="{entry}",{KEY_OPTIONS} {pubkey}'


def generation(cfg: dict) -> str:
    """The name of the path cfg describes: any change to any part is a new one."""
    digest = hashlib.sha256()
    for part in (_text(cfg["entry"]), _text(cfg["dispatcher"]),
                 sudo_rule(cfg["runner_user"], "@DISPATCHER@", cfg["passthrough"]),
                 acceptenv_rule(cfg["runner_user"], cfg["passthrough"]),
                 key_line("@ENTRY@", "@KEY@")):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def entry_path(gen: str) -> str:
    return f"{GENERATIONS}/{gen}/entry"


def dispatcher_path(gen: str) -> str:
    return f"{GENERATIONS}/{gen}/dispatcher"


# --- what is running ----------------------------------------------------------

_COMMAND = re.compile(r'(?:^|,)command="((?:[^"\\]|\\.)*)"')
_GENERATION_ENTRY = re.compile(re.escape(GENERATIONS) + r"/([0-9a-f]{12})/entry")


def key_blob(pub: str) -> str:
    fields = (pub or "").split()
    return fields[1] if len(fields) >= 2 else ""


def line_for(keys: str | None, blob: str) -> str | None:
    """The authorized_keys line carrying the key `blob`, or None."""
    for line in (keys or "").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and blob in line.split():
            return line
    return None


def running_generation(line: str | None) -> str:
    """The generation a key line runs, LEGACY for any other command, NONE with no line."""
    if line is None:
        return NONE
    match = _COMMAND.search(line)
    found = _GENERATION_ENTRY.fullmatch(match.group(1)) if match else None
    return found.group(1) if found else LEGACY


# --- the host -----------------------------------------------------------------

class Host:
    """The filesystem under `root` and the commands the switch runs. Every
    write is atomic (a temporary beside the target, then a rename) and is
    recorded in `wrote`."""

    def __init__(self, root: str = "/", run: Run = _run,
                 chown: Callable[[Path, str], None] = _chown,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.root = Path(root)
        self.run = run
        self.chown = chown
        self.sleep = sleep
        self.wrote: list[str] = []

    def at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def read(self, path: str) -> str | None:
        try:
            return self.at(path).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None

    def write(self, path: str, text: str, mode: int, *, owner: str = "",
              check: list[str] | None = None) -> bool:
        """Write `text` unless the file already holds it. `check` is a command
        the temporary has to pass, its path appended, before it replaces the
        target. Returns whether the file changed."""
        if self.read(path) == text:
            return False
        target = self.at(path)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        if owner:
            self.chown(tmp, owner)
        if check:
            r = self.run([*check, str(tmp)])
            if r.returncode != 0:
                tmp.unlink()
                raise Refused(f"{check[0]} refused {path}: {_tail(r.stdout + r.stderr)}")
        os.replace(tmp, target)
        self.wrote.append(path)
        return True

    def put_back(self, path: str, text: str | None, mode: int, *, owner: str = "") -> None:
        """Return a file to what it held before this run: `text`, or absent."""
        if text is None:
            self.remove(path)
        else:
            self.write(path, text, mode, owner=owner)

    def remove(self, path: str) -> bool:
        target = self.at(path)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        else:
            return False
        self.wrote.append(path)
        return True

    def link(self, path: str, to: str) -> bool:
        target = self.at(path)
        if target.is_symlink() and os.readlink(target) == to:
            return False
        tmp = target.with_name(target.name + ".tmp")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        os.symlink(to, tmp)
        os.replace(tmp, target)
        self.wrote.append(path)
        return True

    def install_generation(self, gen: str, entry: str, dispatcher: str) -> None:
        """The generation's directory, root-owned 0755 with its two
        executables, unless it already holds exactly them."""
        files = {"entry": entry, "dispatcher": dispatcher}
        final = f"{GENERATIONS}/{gen}"
        if all(self.read(f"{final}/{name}") == text for name, text in files.items()):
            return
        base = self.at(GENERATIONS)
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o755)
        tmp = base / f".{gen}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        os.chmod(tmp, 0o755)
        for name, text in files.items():
            (tmp / name).write_text(text, encoding="utf-8")
            os.chmod(tmp / name, 0o755)
        shutil.rmtree(self.at(final), ignore_errors=True)
        os.rename(tmp, self.at(final))
        self.wrote += [f"{final}/{name}" for name in files]

    def generations(self) -> list[str]:
        base = self.at(GENERATIONS)
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir())

    def sshd_reload(self) -> None:
        r = self.run([SYSTEMCTL, "reload", "ssh"])
        if r.returncode != 0:
            raise Refused(f"sshd did not reload: {_tail(r.stderr)}")

    def place_sshd(self, path: str, text: str) -> bool:
        """An sshd drop-in, kept only when sshd accepts the whole config with
        it. The running sshd reads nothing until a reload."""
        before = self.read(path)
        if not self.write(path, text, 0o644):
            return False
        r = self.run([SSHD, "-t"])
        if r.returncode != 0:
            self.put_back(path, before, 0o644)
            raise Refused(f"sshd refused {path}: {_tail(r.stderr)}")
        return True


# --- the proof ----------------------------------------------------------------

def ssh_argv(cfg: dict, key: str, request: str, env: dict[str, str] | None = None) -> list[str]:
    """One dispatch the way the panel sends it: as the runner account, with
    the panel's known_hosts and its ed25519 host key, from loopback."""
    argv = [SSH, "-F", "/dev/null", "-T", "-i", key,
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={cfg['panel_key_dir']}/known_hosts",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"HostKeyAlias={HOST_ALIAS}",
            "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]
    if env:
        argv += ["-o", "SetEnv " + " ".join(f"{k}={v}" for k, v in env.items())]
    return argv + ["-p", "22", f"{cfg['runner_user']}@{LOOPBACK}", request]


def _dispatch(host: Host, argv: list[str]) -> "subprocess.CompletedProcess[str]":
    for attempt in range(ATTEMPTS):
        if attempt:
            host.sleep(DELAY_S)
        r = host.run(argv)
        if r.returncode != 255:
            break
    return r


def prove(host: Host, cfg: dict, key: str, gen: str) -> list[str]:
    """Why a dispatch with `key` does not go through generation `gen`; []
    when it does."""
    names = list(cfg["passthrough"])
    nonce = secrets.token_hex(16)
    r = _dispatch(host, ssh_argv(cfg, key, PROOF_ACTION, {n: nonce for n in names}))
    if r.returncode != 0:
        return [f"the proof dispatch exited {r.returncode}: {_tail(r.stderr)}"]
    reasons = []
    lines = (r.stdout or "").splitlines()
    answer = lines[0] if lines else ""
    if answer != f"{PROOF_ACTION} {gen}":
        reasons.append(f"generation {gen} did not answer the proof (the answer was {answer!r})")
    arrived = {line.split(" ", 1)[1] for line in lines[1:] if line.startswith("arrived ")}
    missing = [n for n in names if n not in arrived]
    if missing:
        reasons.append(f"{', '.join(missing)} did not reach the dispatcher (sshd's "
                       "AcceptEnv or sudo's env_keep dropped them)")
    r = _dispatch(host, ssh_argv(cfg, key, f"{PROOF_ACTION} carried"))
    if r.returncode != 2 or ENTRY_REFUSAL not in (r.stderr or ""):
        reasons.append("a request carrying more than the action name was not refused by "
                       f"the entry before sudo (exit {r.returncode})")
    return reasons


def prove_beside(host: Host, cfg: dict, gen: str, keys_path: str,
                 keys_before: str | None) -> list[str]:
    """The proof through the new generation before the panel's key moves: a
    key minted for this run, authorized from loopback for the new entry alone,
    beside the panel's line, which stays as it is."""
    keydir = tempfile.mkdtemp(prefix="catena-dispatch-proof.", dir=host.at(PROOF_KEY_PARENT))
    try:
        key = f"{keydir}/id_ed25519"
        r = host.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", key])
        if r.returncode != 0:
            return [f"ssh-keygen could not mint the proof key: {_tail(r.stderr)}"]
        pub = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
        keys = keys_before if keys_before is not None else HEADER
        keys = keys if keys.endswith("\n") else keys + "\n"
        host.write(keys_path, keys + key_line(entry_path(gen), pub, loopback_only=True) + "\n",
                   0o600, owner=cfg["runner_user"])
        try:
            return prove(host, cfg, key, gen)
        finally:
            host.put_back(keys_path, keys_before, 0o600, owner=cfg["runner_user"])
    finally:
        shutil.rmtree(keydir, ignore_errors=True)


# --- the switch ---------------------------------------------------------------

def switch(cfg: dict, host: Host) -> dict:
    gen = generation(cfg)
    panel_key = f"{cfg['panel_key_dir']}/id_ed25519"

    def verdict(ok: bool, summary: str) -> dict:
        return {"ok": ok, "changed": bool(host.wrote), "generation": gen,
                "wrote": sorted(set(host.wrote)), "summary": summary}

    panel_pub = (host.read(f"{panel_key}.pub") or "").strip()
    blob = key_blob(panel_pub)
    if not blob:
        return verdict(False, f"the panel's public key {panel_key}.pub is missing or empty; "
                              "the install writes it")
    if not host.at(f"{cfg['runner_home']}/.ssh").is_dir():
        return verdict(False, f"{cfg['runner_home']}/.ssh is missing; the install creates it")

    path = Rendered(cfg, gen, panel_key, panel_pub)
    keys_before = host.read(path.keys)
    running = running_generation(line_for(keys_before, blob))
    current = running
    try:
        if running != gen:
            _move(host, path, keys_before)
            current = gen
        _retire(host, path)
    except Refused as e:
        return verdict(False, f"the panel dispatches through {describe(current)}: {e}")
    if running != gen:
        return verdict(True, f"the panel dispatches through generation {gen}, proven, "
                             f"in place of {describe(running)}")
    if host.wrote:
        return verdict(True, f"generation {gen} is running; its rules, key line and link "
                             "are in place, proven")
    return verdict(True, f"generation {gen} is running; nothing to change")


def describe(running: str) -> str:
    if running == LEGACY:
        return "the path an install wrote"
    if running == NONE:
        return "no path yet"
    return f"generation {running}"


class Rendered:
    """Everything one generation writes, rendered once."""

    def __init__(self, cfg: dict, gen: str, panel_key: str, panel_pub: str) -> None:
        names = list(cfg["passthrough"])
        self.cfg = cfg
        self.gen = gen
        self.user = cfg["runner_user"]
        self.panel_key = panel_key
        self.entry = _text(cfg["entry"])
        self.dispatcher = _text(cfg["dispatcher"])
        self.keys = f"{cfg['runner_home']}/.ssh/authorized_keys"
        self.panel_keys = HEADER + key_line(entry_path(gen), panel_pub) + "\n"
        self.rule = sudo_rule(self.user, dispatcher_path(gen), names)
        self.accept = acceptenv_rule(self.user, names)


def _move(host: Host, path: Rendered, keys_before: str | None) -> None:
    """BESIDE, PROOF and SWITCH. Returns with the panel's line on the new
    generation, proven, or raises Refused with the running path unchanged."""
    gen = path.gen
    reloaded = False

    def undo() -> None:
        host.remove(f"{GENERATIONS}/{gen}")
        host.remove(SUDOERS_NEXT)
        if host.remove(ACCEPTENV_NEXT) or reloaded:
            host.sshd_reload()

    try:
        host.install_generation(gen, path.entry, path.dispatcher)
        host.write(SUDOERS_NEXT, path.rule, 0o440, check=[VISUDO, "-cf"])
        host.place_sshd(ACCEPTENV_NEXT, path.accept)
        # Whether or not the file changed here: a run cut short after writing
        # it may never have reloaded.
        reloaded = True
        host.sshd_reload()
        reasons = prove_beside(host, path.cfg, gen, path.keys, keys_before)
        if reasons:
            raise Refused(f"the proof through generation {gen} failed: {'; '.join(reasons)}")
    except BaseException:
        undo()
        raise

    switched = False
    try:
        host.write(path.keys, path.panel_keys, 0o600, owner=path.user)
        switched = True
        reasons = prove(host, path.cfg, path.panel_key, gen)
        if reasons:
            raise Refused(f"the panel's key did not dispatch through generation {gen} "
                          f"after the switch: {'; '.join(reasons)}")
    except BaseException:
        if switched:
            host.put_back(path.keys, keys_before, 0o600, owner=path.user)
        undo()
        raise


def _retire(host: Host, path: Rendered) -> None:
    """RETIRE, with the generation running. Its rules stay in their -next
    files until the main ones hold them, and the panel's key proves whatever
    changed before anything older is removed."""
    gen = path.gen
    reload = False
    touched = host.write(path.keys, path.panel_keys, 0o600, owner=path.user)
    if host.write(SUDOERS, path.rule, 0o440, check=[VISUDO, "-cf"]):
        touched = True
    if host.place_sshd(ACCEPTENV, path.accept):
        touched = reload = True
    if host.remove(SUDOERS_NEXT):
        touched = True
    if host.remove(ACCEPTENV_NEXT):
        touched = reload = True
    if reload:
        host.sshd_reload()
    if touched:
        reasons = prove(host, path.cfg, path.panel_key, gen)
        if reasons:
            host.write(SUDOERS_NEXT, path.rule, 0o440, check=[VISUDO, "-cf"])
            if host.place_sshd(ACCEPTENV_NEXT, path.accept):
                host.sshd_reload()
            raise Refused(f"generation {gen} did not prove itself once its rules moved "
                          f"into place, so its -next rules are back: {'; '.join(reasons)}")
    host.link(path.cfg["link"], dispatcher_path(gen))
    for other in host.generations():
        if other != gen:
            host.remove(f"{GENERATIONS}/{other}")
    # A run killed during its proof leaves its key directory behind.
    for stale in host.at(PROOF_KEY_PARENT).glob("catena-dispatch-proof.*"):
        shutil.rmtree(stale, ignore_errors=True)


def main() -> int:
    try:
        cfg = json.load(sys.stdin)
    except ValueError as e:
        print(json.dumps({"ok": False, "changed": False, "generation": "", "wrote": [],
                          "summary": f"the input is not JSON: {e}"}))
        return 0
    print(json.dumps(switch(cfg, Host())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
