#!/usr/bin/env python3
"""Prove that the SSH session running this command logged in as the ops account with a key that account accepts.

The install hardens sshd (password and root logins off, every account but
ops refused) only once a session has proved that ops opens with a key:
otherwise the hardening can leave the server with no way in. The installer
reconnects as ops with its key and runs the second leg of the install through
that session; install-host.sh runs this first, with the session's
$SSH_CONNECTION, and refuses the leg when it fails.

The proof is sshd's own record: the journal line "Accepted publickey for
<user> from <client address> port <client port>" for this connection, and
the key fingerprint on it, which must be one of the keys in the account's
authorized_keys. Stdlib only: it runs with the server's python3.

Usage:
  session_proof.py --user ops --ssh-connection "$SSH_CONNECTION"
Exit 0 when proved; 1 with the reason on stderr when not.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import pwd
import re
import subprocess
import sys
from pathlib import Path

# The identifiers sshd logs under: `sshd` before OpenSSH 9.8 split the
# per-connection process out, `sshd-session` from then on (Debian 13).
SSHD_IDENTIFIERS = ("sshd", "sshd-session")

_ACCEPTED = re.compile(
    r"Accepted publickey for (?P<user>\S+) from (?P<addr>\S+) port (?P<port>\d+)"
    r" ssh2: \S+ (?P<fp>SHA256:[A-Za-z0-9+/]+)")
_KEY_TYPE = re.compile(r"^(ssh-|ecdsa-|sk-)")


def fingerprint(blob_b64: str) -> str:
    """OpenSSH's SHA256 fingerprint of a base64 public key blob."""
    digest = hashlib.sha256(base64.b64decode(blob_b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def authorized_fingerprints(text: str) -> set[str]:
    """The fingerprints of the keys in an authorized_keys file. A line may
    start with options, which can hold quoted spaces, so the key is found as
    the token after its type rather than by position."""
    out: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = re.findall(r'(?:[^\s"]|"[^"]*")+', line)
        for i, token in enumerate(tokens[:-1]):
            if _KEY_TYPE.match(token):
                try:
                    out.add(fingerprint(tokens[i + 1]))
                except ValueError:
                    pass
                break
    return out


def accepted_fingerprint(journal: str, user: str, addr: str, port: str) -> str:
    """The key fingerprint sshd accepted for this user and client endpoint,
    or "" when the journal records no such login. The last one wins: a
    client port is reused only across connections."""
    found = ""
    for match in _ACCEPTED.finditer(journal):
        if (match["user"], match["addr"], match["port"]) == (user, addr, port):
            found = match["fp"]
    return found


def prove(user: str, ssh_connection: str, journal: str,
          authorized_keys: str) -> tuple[bool, str]:
    parts = ssh_connection.split()
    if len(parts) != 4:
        return False, (f"no SSH session to prove: SSH_CONNECTION is "
                       f"{ssh_connection!r}")
    addr, port = parts[0], parts[1]
    accepted = accepted_fingerprint(journal, user, addr, port)
    if not accepted:
        return False, (f"sshd recorded no key login for {user} from {addr} "
                       f"port {port}: this session did not log in as {user} "
                       f"with a key")
    if accepted not in authorized_fingerprints(authorized_keys):
        return False, (f"{user} logged in with {accepted}, which is not in "
                       f"{user}'s authorized_keys")
    return True, f"{user} logged in from {addr} with {accepted}"


def _journal() -> str:
    argv = ["journalctl", "--no-pager", "-o", "cat", "--since", "-2h"]
    for ident in SSHD_IDENTIFIERS:
        argv += ["-t", ident]
    return subprocess.run(argv, capture_output=True, text=True,
                          check=False).stdout


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--user", required=True)
    ap.add_argument("--ssh-connection", required=True)
    args = ap.parse_args(argv)
    try:
        home = Path(pwd.getpwnam(args.user).pw_dir)
        keys = (home / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    except (KeyError, OSError) as exc:
        print(f"session_proof: cannot read {args.user}'s authorized_keys: {exc}",
              file=sys.stderr)
        return 1
    ok, why = prove(args.user, args.ssh_connection, _journal(), keys)
    print(f"session_proof: {why}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
