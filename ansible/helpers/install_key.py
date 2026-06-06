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
    python3 helpers/install_key.py <host> [--user=<user>] [--pubkey=<path>] [--env-file=<path>]

    <host>      Public IP or resolvable hostname of the fresh VPS
    --user      Initial SSH user the provider supplied. If not given, the
                script prompts (default: root). Common values:
                  root      OVH (most images), Servarica, Contabo, Hetzner
                  debian    OVH (some Debian images)
                  ubuntu    AWS, Vultr Ubuntu, some others
                  ec2-user  Amazon Linux
    --pubkey    Public key to install (default: $SSH_PUBLIC_KEY_FILE from .env,
                then ~/.ssh/catena_ed25519.pub as a last resort)
    --env-file  Path to the .env file to read SSH_PUBLIC_KEY_FILE / SSH_PRIVATE_KEY
                from (default: inventory/example/.env, then ansible-root .env).
                Use this when running against a non-default inventory.

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
from pathlib import Path

import pexpect

REPO_ROOT = Path(__file__).resolve().parent.parent

# Search order for the .env when --env-file isn't passed. Mirrors the priority
# in playbooks/lookup_plugins/dotenv.py: per-inventory first, repo-root fallback for
# legacy single-inventory installs that haven't migrated.
DEFAULT_DOTENV_CANDIDATES = (
    REPO_ROOT / "ansible" / "inventory" / "dev" / ".env",
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
    candidate = env.get("SSH_PUBLIC_KEY_FILE") or "~/.ssh/catena_ed25519.pub"
    return os.path.expanduser(candidate)


def _default_privkey_path(env: dict[str, str]) -> str:
    candidate = env.get("SSH_PRIVATE_KEY") or "~/.ssh/catena_ed25519"
    return os.path.expanduser(candidate)


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^&*_+-="
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _key_already_works(host: str, user: str, privkey: str) -> bool:
    """Return True if we can already ssh in with just the key.

    When `privkey` points at a file the caller can read, ssh pins to it
    via `-i` + `IdentitiesOnly=yes` to avoid drifting onto an unrelated
    agent identity. When the file is unreadable (e.g. running inside the
    Semaphore worker container, where the host's 0600 key files are not
    accessible to the container user), fall through to whatever
    identities `$SSH_AUTH_SOCK` offers -- the authorised key is the
    same, the auth path is just agent-mediated.
    """
    args = [
        "ssh",
        "-o", "PreferredAuthentications=publickey",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
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


def _install_via_interactive_ssh(
    host: str,
    user: str,
    password: str,
    pubkey_line: str,
) -> None:
    """Drive an interactive SSH session to install the key, handling password
    change if the server demands one."""
    # Capture the full session in-memory so we can dump a diagnostic tail
    # on TIMEOUT / EOF. The buffer never hits disk -- we redact anything
    # near a "password:" token before printing. Safe-ish for an initial
    # provider password that's about to be replaced anyway.
    import io
    session_log = io.StringIO()
    child = pexpect.spawn(
        "ssh",
        [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            f"{user}@{host}",
        ],
        timeout=30,
        encoding="utf-8",
    )
    child.logfile_read = session_log

    try:
        _drive_ssh_session(child, password, pubkey_line, host, user)
    except (pexpect.EOF, pexpect.TIMEOUT):
        _dump_session_tail(session_log, password=password)
        raise


def _drive_ssh_session(child, password, pubkey_line, host, user):
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
    # entirely and handles arbitrarily long banners.
    # Early-warning patterns that fire BEFORE the actual "Current password:"
    # prompt -- OVH's Debian image prints these at login-time, then streams a
    # ~10s MOTD, then shows "Current password:". Matching the early warnings
    # lets us wait for the real prompt without timing out mid-banner.
    change_warnings = [
        r"required to change your password",
        r"[Pp]assword has expired",
        r"must change your password",
        r"Changing password for",
    ]
    current_pw_prompts = [
        r"[Cc]urrent.*password:",
        r"UNIX password:",
        r"\(current\)\s*UNIX password:",
    ]
    new_password_prompts = [r"[Nn]ew password:", r"[Nn]ew UNIX password:"]
    retype_prompts = [r"[Rr]etype new password:", r"[Rr]e-?enter new password:"]
    auth_failed = [r"Permission denied", r"Authentication failed"]

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
                _install_via_interactive_ssh(host, user, new_pw, pubkey_line)
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
                _install_via_interactive_ssh(host, user, new_pw, pubkey_line)
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
    # bash-syntax-invalid, and bash drops it without a visible error.
    # The previous inline form embedded pubkey_line TWICE + sudo + the
    # matcher + tee + redirects + marker -> ~330 chars; auth.log
    # showed mkdir/chmod ran but the `sudo tee` part of the second
    # command never fired (key was never written, /root/.ssh/
    # authorized_keys stayed empty, _key_already_works returned
    # False, install_key.py exited rc=5 -- "key install completed but
    # root key auth verification failed"). Bench-39 reproduced this
    # 4 runs in a row.
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
        help="Path to the .env file to read SSH_PUBLIC_KEY_FILE / SSH_PRIVATE_KEY from "
             "(default: inventory/dev/.env, then repo-root .env).",
    )
    ap.add_argument(
        "--pubkey",
        default=None,
        help="Public key file to install (default: from .env or ~/.ssh/catena_ed25519.pub)",
    )
    ap.add_argument(
        "--privkey",
        default=None,
        help="Private key for the final verification (default: from .env)",
    )
    args = ap.parse_args()

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

    # Scrub any stale known_hosts entry for this IP. Reinstall-with-same-IP
    # is a common case (operator wipes + reinstalls a VPS to re-test the
    # installer); without this, SSH aborts with "REMOTE HOST IDENTIFICATION
    # HAS CHANGED" before auth even starts, and the script sees an immediate
    # EOF with no usable error. StrictHostKeyChecking=accept-new accepts
    # NEW hosts silently but still rejects CHANGED hosts -- so we have to
    # actively clear the stale entry first. Idempotent: if there's no
    # entry, ssh-keygen -R is a no-op.
    known_hosts = Path("~/.ssh/known_hosts").expanduser()
    if known_hosts.is_file():
        subprocess.run(
            ["ssh-keygen", "-f", str(known_hosts), "-R", args.host],
            capture_output=True,
            text=True,
        )

    # Shortcut: if key auth already works for the initial user or the ops
    # user (created by bootstrap Phase 1), there's nothing to do.
    privkey_expanded = os.path.expanduser(privkey_arg)
    ops_user = env.get("OPS_USER", "ops")
    for check_user in dict.fromkeys([user, ops_user]):  # dedupe
        if _key_already_works(args.host, check_user, privkey_expanded):
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
        )
    except (pexpect.EOF, pexpect.TIMEOUT) as exc:
        # Session tail was already printed by _install_via_interactive_ssh.
        print(f"SSH session ended unexpectedly: {type(exc).__name__}", file=sys.stderr)
        print(
            "If the tail above doesn't reveal what happened, fall back to manual SSH:",
            file=sys.stderr,
        )
        print(f"  ssh {user}@{args.host}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 4

    # Verify: key auth now works for the user we installed it for.
    if _key_already_works(args.host, user, os.path.expanduser(privkey_arg)):
        print(f"✓ Key installed and verified for {user}@{args.host}. You can now run:")
        print("    ansible-playbook playbooks/bootstrap.yml --limit <inventory_host_name>")
        return 0
    else:
        print(
            f"Key install completed but {user}@{args.host} key auth verification failed. "
            "Check logs; try manual SSH.",
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
