"""Send Keycloak's verify email to one user, through kcadm.sh in the Keycloak
server container, and retry while the mail could not reach the relay.

kcadm answers a failed send with a generic "Failed to send verify email"; the
cause is in the Keycloak server's log. A send whose log shows the relay's name
not resolving or its port not answering (UnknownHostException,
MailConnectException) is tried again, up to --tries times, --delay seconds
apart: one lookup lost by Docker's embedded DNS fails one send, not the
install. A send the relay refused (authentication, sender or recipient
rejected) fails at once, because trying again gets the same answer.

    keycloak_send_verify_email.py --container <name> --user-id <id> --realm vps

exits with kcadm's last exit code and passes on its output; each retry says
why on stderr.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

KCADM = "/opt/keycloak/bin/kcadm.sh"
UNREACHED = re.compile(r"UnknownHostException|MailConnectException")
REFUSED = re.compile(
    r"AuthenticationFailedException|SMTPSendFailedException|SMTPAddressFailedException")


def unreached(log: str) -> bool:
    """Whether a failed send's log shows the relay unreached and not refusing."""
    return bool(UNREACHED.search(log)) and not REFUSED.search(log)


def _server_log(container: str, since: int) -> str:
    out = subprocess.run(["docker", "logs", "--since", str(since), container],
                         capture_output=True, text=True, check=False)
    return out.stdout + out.stderr


def send(container: str, user_id: str, realm: str, tries: int, delay: float) -> int:
    for attempt in range(1, tries + 1):
        started = int(time.time())
        out = subprocess.run(
            ["docker", "exec", container, KCADM, "update",
             f"users/{user_id}/send-verify-email", "-r", realm],
            capture_output=True, text=True, check=False)
        if out.returncode == 0 or attempt == tries or not unreached(
                _server_log(container, started)):
            sys.stdout.write(out.stdout)
            sys.stderr.write(out.stderr)
            return out.returncode
        print(f"attempt {attempt}: the mail relay was not reached; "
              f"trying again in {delay:g} s", file=sys.stderr)
        time.sleep(delay)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--container", required=True)
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--realm", required=True)
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--delay", type=float, default=5.0)
    args = ap.parse_args(argv)
    return send(args.container, args.user_id, args.realm, args.tries, args.delay)


if __name__ == "__main__":
    sys.exit(main())
