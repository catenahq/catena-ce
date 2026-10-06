#!/usr/bin/env python3
"""
Automate the pre-bootstrap manual-SSH step across providers.

What this script replaces:
  Manually SSHing into a fresh VPS, navigating whatever first-login quirks the
  provider insists on (OVH's pre-expired password, EULA banners, etc.), and
  pasting the operator's dedicated public key into /root/.ssh/authorized_keys.

How it stays provider-agnostic:
  It doesn't know or care what the provider is. It connects with a password,
  handles any password-change prompt it encounters, installs the key, and
  verifies key auth. Providers that already accept a key at create-time fail
  the first test fast and the script exits cleanly ("key already works").

Usage (the installer / bootstrap.yml invoke this for you):
    python3 helpers/install_key.py <host> [--user=<user>] [--port=<port>]
                                   [--pubkey=<path>] [--env-file=<path>]
    python3 helpers/install_key.py <host> --user=<user> [--port=<port>] --check-password

    <host>      Public IP or resolvable hostname of the fresh VPS
    --user      Initial SSH user the provider supplied. If not given, the
                script prompts (default: root). Common values:
                  root      OVH (most images), Servarica, Contabo, Hetzner
                  debian    OVH (some Debian images)
                  ubuntu    AWS, Vultr Ubuntu, some others
                  ec2-user  Amazon Linux
    --port      The SSH port (default 22)
    --pubkey    Public key to install (default: $SSH_PRIVATE_KEY from .env plus
                .pub, then ~/.ssh/catena_ed25519.pub as a last resort)
    --env-file  Path to the .env file to read SSH_PRIVATE_KEY from (default:
                inventory/dev/.env, then ansible/.env).
                Use this when running against a non-default inventory.
    --check-password
                Only log in with the password and say whether it opens the
                account; install nothing and change nothing. The graphical
                installer asks this before an install. A password the server
                wants changed at first login counts as opening it: the install
                changes it.

Only third-party dep is pexpect. The password is read from stdin (no echo).
If the server forces a password
change, a fresh random one is generated and typed in -- it's never used again
because after this script runs, the VPS is key-only for root, and bootstrap.yml
goes on to harden sshd further.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

import pexpect

REPO_ROOT = Path(__file__).resolve().parent.parent

# Post-install key-auth verify retry budget. A freshly booted provider VM
# can accept the interactive password install a beat before its sshd serves
# the new authorized_keys on a fresh connection; a single immediate probe
# then races and returns rc=5 even though the key is in place (seen on the
# bench migrate target under load). Retry a few times before failing.
_VERIFY_ATTEMPTS = 5
_VERIFY_DELAY_S = 3.0

# What a server says after the password, by provider shape. Early warnings
# fire BEFORE the actual "Current password:" prompt -- OVH's Debian image prints
# them at login, streams a ~10s MOTD, then asks.
_CHANGE_WARNINGS = [
    r"required to change your password",
    r"[Pp]assword has expired",
    r"must change your password",
    r"Changing password for",
]
_CURRENT_PW_PROMPTS = [
    r"[Cc]urrent.*password:",
    r"UNIX password:",
    r"\(current\)\s*UNIX password:",
]
_NEW_PASSWORD_PROMPTS = [r"[Nn]ew password:", r"[Nn]ew UNIX password:"]
_RETYPE_PROMPTS = [r"[Rr]etype new password:", r"[Rr]e-?enter new password:"]
_AUTH_FAILED = [r"Permission denied", r"Authentication failed"]

# Search order for the .env when --env-file isn't passed. Mirrors the priority
# in playbooks/lookup_plugins/dotenv.py: per-inventory first, repo-root
# fallback for a single-inventory checkout.
DEFAULT_DOTENV_CANDIDATES = (
    REPO_ROOT / "inventory" / "dev" / ".env",
    REPO_ROOT / ".env",
)


def _resolve_dotenv(explicit: str | None) -> Path | None:
    if explicit:
        return Path(os.path.expanduser(explicit))
    for candidate in DEFAULT_DOTENV_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _read_dotenv(path: Path | None) -> dict[str, str]:
    """Minimal dotenv parser -- mirrors playbooks/lookup_plugins/dotenv.py semantics."""
    if path is None or not path.is_file():
        return {}
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key.strip()] = value
    return result


def _default_pubkey_path(env: dict[str, str]) -> str:
    return _default_privkey_path(env) + ".pub"


def _default_privkey_path(env: dict[str, str]) -> str:
    candidate = env.get("SSH_PRIVATE_KEY") or "~/.ssh/catena_ed25519"
    return os.path.expanduser(candidate)


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^&*_+-="
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _key_already_works(host: str, user: str, privkey: str, port: int = 22) -> bool:
    """Return True if we can already ssh in with just the key.

    When `privkey` points at a file the caller can read, ssh pins to it
    via `-i` + `IdentitiesOnly=yes` to avoid drifting onto an unrelated
    agent identity. When the file is unreadable (e.g. running inside an
    automation container, where the host's 0600 key files are not
    accessible to the container user), fall through to whatever
    identities `$SSH_AUTH_SOCK` offers -- the authorised key is the
    same, the auth path is just agent-mediated.
    """
    args = [
        "ssh",
        "-o", "PreferredAuthentications=publickey",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        # Do NOT consult / write the controller's persistent known_hosts.
        # Key auth is verified on a freshly (re)created machine, so there is
        # no host-key continuity to protect. A recycled provider IP that
        # still carries a PREVIOUS VM's host key in known_hosts makes
        # accept-new reject the connection ("REMOTE HOST IDENTIFICATION HAS
        # CHANGED") and fail an otherwise-good install.
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-p", str(port),
    ]
    if privkey and os.access(privkey, os.R_OK):
        args.extend(["-i", privkey, "-o", "IdentitiesOnly=yes"])
    args.extend([f"{user}@{host}", "true"])
    result = subprocess.run(args, capture_output=True, text=True)
    return result.returncode == 0


def _dump_session_tail(session_log, password: str | None = None) -> None:
    """On pexpect failure, print the last ~3 KB of the ssh session to stderr
    so the operator can see what the script was choking on. Redact any
    occurrences of the literal `password` value (defense in depth) AND any
    "password:"-prefixed lines. Sent to stderr so it's captured by Ansible
    output.

    Two-layer redaction:
      1. The literal password value (if provided) is replaced everywhere
         it appears in the tail. This is the load-bearing redact: it
         catches password echoes regardless of where they land
         (multi-line prompts, "Re-enter new UNIX password:" lines longer
         than the regex would match, post-prompt error messages that
         quote what was sent, etc.).
      2. A backup regex that masks anything after a "password:" prompt --
         catches cases where the script never knew the literal password
         (e.g. a future code path that drives only the change-password
         flow without holding the new password as a variable). The bound
         was tightened from `{0,40}?` to `[^\\n]*?` so long prompts no
         longer slip the redact.
    """
    if session_log is None:
        return
    tail = session_log.getvalue()[-3000:]
    if password:
        # Literal redact first -- catches any echo regardless of context.
        # Only do the replace if the password is at least 4 chars; a
        # 1-3 char password is too short to be meaningful AND replacing
        # short tokens would clobber unrelated text.
        if len(password) >= 4:
            tail = tail.replace(password, "[REDACTED]")
    import re as _re
    tail = _re.sub(r"(?i)(password[^\n]*?:)\s*\S+",
                   r"\1 [REDACTED]", tail)
    print("── ssh session tail (last 3 KB, passwords redacted) ──", file=sys.stderr)
    print(tail, file=sys.stderr)
    print("── end session tail ──", file=sys.stderr)


def _spawn_password_ssh(host: str, user: str, port: int):
    """An interactive ssh that authenticates by password only."""
    return pexpect.spawn(
        "ssh",
        [
            "-o", "StrictHostKeyChecking=accept-new",
            # Same rationale as the verify probe: a recycled provider IP
            # carrying a prior VM's host key must not block the password
            # install with a host-key-changed error. Fresh VM, no continuity.
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-p", str(port),
            f"{user}@{host}",
        ],
        timeout=30,
        encoding="utf-8",
    )


def _password_opens(host: str, user: str, password: str, port: int = 22) -> tuple[bool, str]:
    """Whether `password` logs `user` in. Installs nothing and changes nothing:
    a server that asks for a new password at first login is left asking, and
    counts as opened, because the install answers that prompt itself."""
    child = _spawn_password_ssh(host, user, port)
    try:
        if child.expect([r"[Pp]assword:"] + _AUTH_FAILED + [pexpect.EOF]) != 0:
            return False, f"{user}@{host} takes no password"
        child.sendline(password)
        prompts = (_CHANGE_WARNINGS + _CURRENT_PW_PROMPTS + _NEW_PASSWORD_PROMPTS)
        i = child.expect(prompts + _AUTH_FAILED + [pexpect.EOF, pexpect.TIMEOUT],
                         timeout=10)
        if i < len(prompts):
            return True, (f"the password opens {user}@{host}; the server asks for "
                          "a new one at first login, which the install sets")
        if i < len(prompts) + len(_AUTH_FAILED):
            return False, f"{user}@{host} refused the password"
        if i == len(prompts) + len(_AUTH_FAILED):
            return False, f"{user}@{host} closed the connection after the password"
        marker = "__CATENA_CHECK_" + secrets.token_hex(6) + "__"
        child.sendline(f"echo {marker}")
        child.expect_exact(marker, timeout=30)
        return True, f"the password opens {user}@{host}"
    except (pexpect.EOF, pexpect.TIMEOUT) as exc:
        return False, f"no answer from {user}@{host}: {type(exc).__name__}"
    finally:
        child.close(force=True)


def _install_via_interactive_ssh(
    host: str,
    user: str,
    password: str,
    pubkey_line: str,
    port: int = 22,
) -> None:
    """Drive an interactive SSH session to install the key, handling password
    change if the server demands one."""
    # Capture the full session in-memory so we can dump a diagnostic tail
    # on TIMEOUT / EOF. The buffer never hits disk -- we redact anything
    # near a "password:" token before printing. Safe-ish for an initial
    # provider password that's about to be replaced anyway.
    import io
    session_log = io.StringIO()
    child = _spawn_password_ssh(host, user, port)
    child.logfile_read = session_log

    try:
        _drive_ssh_session(child, password, pubkey_line, host, user, port)
    except (pexpect.EOF, pexpect.TIMEOUT):
        _dump_session_tail(session_log, password=password)
        raise


def _drive_ssh_session(child, password, pubkey_line, host, user, port):
    """Body of the ssh session drive -- extracted so we can wrap it in
    a try/except that prints the diagnostic session tail on failure."""
    # 1. answer the initial password prompt
    child.expect([r"[Pp]assword:"])
    child.sendline(password)

    # 2. what happens next depends on the provider:
    #      (a) shell prompt (most common)
    #      (b) password-change prompt (OVH initial passwords, some Debian images)
    #      (c) long MOTD / legal banner that STREAMS for 10+s before a prompt
    #      (d) auth failure
    # Shell-prompt shape is unreliable (color codes, custom PS1, OVH's verbose
    # banner) -- matching "$" or "#" after banner text fails randomly. Use a
    # marker-command probe instead: wait long enough for any password-change
    # prompt or auth failure to surface, then actively synchronise by echoing
    # a unique marker and expecting it back. That avoids guessing prompt shape
    # entirely and handles arbitrarily long banners. Matching the early
    # warnings (_CHANGE_WARNINGS) lets us wait for the real prompt without
    # timing out mid-banner.
    change_warnings = _CHANGE_WARNINGS
    current_pw_prompts = _CURRENT_PW_PROMPTS
    new_password_prompts = _NEW_PASSWORD_PROMPTS
    retype_prompts = _RETYPE_PROMPTS
    auth_failed = _AUTH_FAILED

    def _wait_for_shell(c):
        """Synchronise on a unique marker we echo. Returns when the marker has
        been seen in the output stream -- we're now at a shell prompt no matter
        what shape or banner preceded it."""
        marker = "__VPSDEPLOY_READY_" + secrets.token_hex(6) + "__"
        # Two newlines: first flushes any buffered banner lines, second triggers
        # a fresh prompt (most shells echo the prompt after \n).
        c.sendline("")
        c.sendline(f"echo {marker}")
        c.expect_exact(marker, timeout=60)
        # Absorb the trailing newline + next prompt so subsequent sendlines
        # are clean.
        c.expect([r"\r\n", pexpect.TIMEOUT], timeout=2)

    # Race: look for SIX classes of signal within 10s:
    #   (a) change_warnings  -- early banner says pw expired; real prompt comes later
    #   (b) current_pw_prompts -- actual "Current password:" asks for re-type
    #   (c) new_password_prompts -- straight to "New password:"
    #   (d) auth_failed -- bad password
    # If (a) fires, wait up to another 30s for the real current/new prompt
    # (banner can stream ~10-15s on OVH's verbose Debian image).
    # If none fire at all, we're likely at a shell -- use marker probe.
    try:
        i = child.expect(
            change_warnings + current_pw_prompts + new_password_prompts + auth_failed,
            timeout=10,
        )
    except pexpect.TIMEOUT:
        _wait_for_shell(child)
    else:
        n_warn = len(change_warnings)
        n_cur = len(current_pw_prompts)
        n_new = len(new_password_prompts)

        if i < n_warn:
            # Early warning fired -- real prompt comes after MOTD. Wait up to
            # another 30s for the Current / New password prompt.
            j = child.expect(
                current_pw_prompts + new_password_prompts + auth_failed,
                timeout=30,
            )
            # Re-bucket j into the same flow as the direct match below.
            i = n_warn + j  # treat as if we'd hit current_pw_prompts or later

        # Indices now:
        #   [0, n_warn)                           -> change_warnings (handled above)
        #   [n_warn, n_warn+n_cur)                -> current_pw_prompts
        #   [n_warn+n_cur, n_warn+n_cur+n_new)    -> new_password_prompts
        #   else                                  -> auth_failed
        if n_warn <= i < n_warn + n_cur:
            child.sendline(password)
            child.expect(new_password_prompts, timeout=15)
            new_pw = _random_password()
            child.sendline(new_pw)
            child.expect(retype_prompts, timeout=15)
            child.sendline(new_pw)
            try:
                child.expect([pexpect.EOF], timeout=10)
                child.close()
                _install_via_interactive_ssh(host, user, new_pw, pubkey_line, port)
                return
            except pexpect.TIMEOUT:
                _wait_for_shell(child)
        elif n_warn + n_cur <= i < n_warn + n_cur + n_new:
            new_pw = _random_password()
            child.sendline(new_pw)
            child.expect(retype_prompts, timeout=15)
            child.sendline(new_pw)
            try:
                child.expect([pexpect.EOF], timeout=10)
                child.close()
                _install_via_interactive_ssh(host, user, new_pw, pubkey_line, port)
                return
            except pexpect.TIMEOUT:
                _wait_for_shell(child)
        else:
            child.close()
            raise RuntimeError("authentication failed -- wrong password?")

    # 3. we have a shell (synchronised via marker). Install the key
    # idempotently. Each command MUST stay under Linux's pty
    # canonical-mode line limit (MAX_CANON, 255 bytes) -- exceed it
    # and the line is silently truncated, the truncated half is
    # bash-syntax-invalid, and bash drops it without a visible error. A
    # single combined command (pubkey_line embedded twice + sudo + the
    # matcher + tee + redirects + marker, ~330 chars) truncates right
    # where the `sudo tee` half sits: mkdir/chmod run in auth.log, the
    # key is never written, /root/.ssh/authorized_keys stays empty,
    # _key_already_works returns False, and install_key.py exits rc=5
    # ("key install completed but root key auth verification failed").
    #
    # Stage the pubkey to a temp file first so the idempotency check
    # + append commands reference it by path. Every command stays
    # well under MAX_CANON regardless of key length.
    keyfile = f"/tmp/.catena-key-{secrets.token_hex(4)}"
    # Install location depends on who is connecting:
    # - root: key goes into /root/.ssh (bootstrap Phase 1 connects as root).
    # - non-root user (debian, ubuntu, ...): key goes into that user's ~/.ssh;
    #   bootstrap Phase 1 connects as this user with become: true so the
    #   common role runs as root. No root login is opened at any point.
    if user == "root":
        ssh_dir_cmd = "install -d -m 700 /root/.ssh"
        auth_keys = "/root/.ssh/authorized_keys"
    else:
        ssh_dir_cmd = "install -d -m 700 ~/.ssh"
        auth_keys = "~/.ssh/authorized_keys"
    commands = [
        # Stage key to temp file (short: ~95-char key + 30-char wrapper).
        f"echo '{pubkey_line}' > {keyfile}",
        ssh_dir_cmd,
        # Idempotent append, referencing temp file -- line stays short
        # regardless of key length.
        f"sh -c 'grep -qxF \"$(cat {keyfile})\" {auth_keys} 2>/dev/null || cat {keyfile} >> {auth_keys}'",
        f"chmod 600 {auth_keys}",
        f"rm -f {keyfile}",
    ]
    # Sync after each command via a per-command marker -- same trick as
    # _wait_for_shell. Avoids prompt-shape matching entirely.
    for cmd in commands:
        marker = "__VPSDEPLOY_CMD_" + secrets.token_hex(6) + "__"
        child.sendline(f"{cmd}; echo {marker}")
        child.expect_exact(marker, timeout=30)

    child.sendline("exit")
    child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=10)
    child.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", help="Public IP or hostname of the fresh VPS")
    ap.add_argument(
        "--user",
        default=None,
        help="Initial SSH user (provider-dependent: root, debian, ubuntu, ec2-user...). "
             "If omitted, the script prompts for it.",
    )
    ap.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Path to the .env file to read SSH_PRIVATE_KEY from "
             "(default: inventory/dev/.env, then ansible/.env).",
    )
    ap.add_argument(
        "--pubkey",
        default=None,
        help="Public key file to install (default: the private key's .pub)",
    )
    ap.add_argument(
        "--privkey",
        default=None,
        help="Private key for the final verification (default: from .env)",
    )
    ap.add_argument("--port", type=int, default=22, help="SSH port (default 22)")
    ap.add_argument(
        "--check-password",
        action="store_true",
        help="Only say whether the password opens --user; install nothing.",
    )
    args = ap.parse_args()

    if args.check_password:
        if not args.user:
            print("--check-password needs --user", file=sys.stderr)
            return 2
        password = os.environ.get("INSTALL_KEY_PASSWORD") or getpass.getpass(
            f"Initial password for {args.user}@{args.host}: ")
        opened, why = _password_opens(args.host, args.user, password, args.port)
        print(why)
        return 0 if opened else 4

    env = _read_dotenv(_resolve_dotenv(args.env_file))
    pubkey_arg = args.pubkey or _default_pubkey_path(env)
    privkey_arg = args.privkey or _default_privkey_path(env)

    # Ask for the initial SSH user if not provided on CLI. Providers don't
    # agree (root for most cheap VPS, debian for some OVH images, ubuntu for
    # AWS, etc.) -- defaulting silently to root is a footgun.
    if args.user:
        user = args.user
    else:
        prompt_default = "root"
        entered = input(f"Initial SSH user [{prompt_default}]: ").strip()
        user = entered or prompt_default

    pubkey_path = Path(os.path.expanduser(pubkey_arg))
    if not pubkey_path.is_file():
        print(f"Public key not found: {pubkey_path}", file=sys.stderr)
        print(
            "Generate one with:  ssh-keygen -t ed25519 -f ~/.ssh/catena_ed25519 -C catena",
            file=sys.stderr,
        )
        return 2

    pubkey_line = pubkey_path.read_text().strip()
    if "\n" in pubkey_line:
        pubkey_line = pubkey_line.splitlines()[0]

    # Shortcut: if key auth already works for the initial user or the ops
    # user (created by bootstrap Phase 1), there's nothing to do.
    privkey_expanded = os.path.expanduser(privkey_arg)
    ops_user = env.get("OPS_USER", "ops")
    for check_user in dict.fromkeys([user, ops_user]):  # dedupe
        if _key_already_works(args.host, check_user, privkey_expanded, args.port):
            print(f"✓ Key auth already works for {check_user}@{args.host}. Nothing to install.")
            return 0

    print(f"Installing {pubkey_path} on {user}@{args.host}")
    # Accept password via env var (INSTALL_KEY_PASSWORD) so the installer /
    # bootstrap.yml can pass the provider's initial password through without
    # a second interactive prompt. Falls through to getpass for manual
    # invocation.
    password = os.environ.get("INSTALL_KEY_PASSWORD") or getpass.getpass(
        f"Initial password for {user}@{args.host}: "
    )

    try:
        _install_via_interactive_ssh(
            args.host,
            user,
            password,
            pubkey_line,
            args.port,
        )
    except (pexpect.EOF, pexpect.TIMEOUT) as exc:
        # Session tail was already printed by _install_via_interactive_ssh.
        print(f"SSH session ended unexpectedly: {type(exc).__name__}", file=sys.stderr)
        print(
            "If the tail above doesn't reveal what happened, fall back to manual SSH:",
            file=sys.stderr,
        )
        print(f"  ssh -p {args.port} {user}@{args.host}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 4

    # Verify: key auth now works for the user we installed it for. Retry a
    # few times -- a just-booted VM's sshd can serve the new authorized_keys
    # a beat after the interactive install returns, and a single immediate
    # probe would race that window and fail an otherwise-good install.
    privkey_expanded = os.path.expanduser(privkey_arg)
    for attempt in range(_VERIFY_ATTEMPTS):
        if _key_already_works(args.host, user, privkey_expanded, args.port):
            print(f"✓ Key installed and verified for {user}@{args.host}. You can now run:")
            print("    ansible-playbook playbooks/bootstrap.yml --limit <inventory_host_name>")
            return 0
        if attempt < _VERIFY_ATTEMPTS - 1:
            time.sleep(_VERIFY_DELAY_S)
    print(
        f"Key install completed but {user}@{args.host} key auth verification failed "
        f"after {_VERIFY_ATTEMPTS} attempts. Check logs; try manual SSH.",
        file=sys.stderr,
    )
    return 5


def _entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\nCancelled (interrupted).", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
