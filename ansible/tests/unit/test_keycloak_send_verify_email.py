"""helpers/keycloak_send_verify_email.py tries the validate play's verify email
again only while the Keycloak worker log shows the mail relay unreached, so one
lost DNS lookup does not fail the install and a refused mail still does.

Run: uv run pytest tests/unit/test_keycloak_send_verify_email.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import yaml

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import keycloak_send_verify_email as ksv

LOST_LOOKUP = ("ERROR [org.keycloak.email] Failed to send email: MailConnectException: "
               "Couldn't connect to host, port: smtp-relay.brevo.com, 587; "
               "java.net.UnknownHostException: smtp-relay.brevo.com")
BAD_LOGIN = ("ERROR [org.keycloak.email] Failed to send email: "
             "jakarta.mail.AuthenticationFailedException: 535 5.7.8 Authentication failed")


def _run(sends: list[int], logs: list[str]):
    """subprocess.run answering each kcadm send with the next exit code and
    each docker logs with the next log text."""
    sends, logs = iter(sends), iter(logs)

    def run(argv, **_):
        if argv[1] == "exec":
            return subprocess.CompletedProcess(argv, next(sends), "", "")
        return subprocess.CompletedProcess(argv, 0, next(logs), "")
    return mock.Mock(side_effect=run)


def _send(sends: list[int], logs: list[str]):
    run = _run(sends, logs)
    with mock.patch.object(ksv.subprocess, "run", run), \
            mock.patch.object(ksv.time, "sleep") as sleep:
        rc = ksv.send("kc", "uid", "vps", tries=3, delay=5)
    return rc, [c.args[0][1] for c in run.call_args_list], sleep


def test_a_lost_lookup_is_tried_again():
    rc, calls, sleep = _send([1, 0], [LOST_LOOKUP])
    assert rc == 0
    assert calls == ["exec", "logs", "exec"]
    sleep.assert_called_once_with(5)


def test_a_refused_mail_fails_at_once():
    rc, calls, sleep = _send([1], [BAD_LOGIN])
    assert rc == 1
    assert calls == ["exec", "logs"]
    sleep.assert_not_called()


def test_a_refusal_beside_a_connect_error_is_not_tried_again():
    rc, calls, _ = _send([1], [LOST_LOOKUP + "\n" + BAD_LOGIN])
    assert rc == 1
    assert calls == ["exec", "logs"]


def test_a_relay_never_reached_fails_after_the_last_try():
    rc, calls, sleep = _send([1, 1, 1], [LOST_LOOKUP, LOST_LOOKUP])
    assert rc == 1
    assert calls == ["exec", "logs", "exec", "logs", "exec"]
    assert sleep.call_count == 2


def test_the_log_read_starts_at_the_send():
    run = _run([1, 0], [LOST_LOOKUP])
    with mock.patch.object(ksv.subprocess, "run", run), \
            mock.patch.object(ksv.time, "sleep"), \
            mock.patch.object(ksv.time, "time", return_value=1700000000.7):
        ksv.send("kc", "uid", "vps", tries=3, delay=5)
    assert run.call_args_list[1].args[0] == ["docker", "logs", "--since", "1700000000", "kc"]


def test_the_validate_play_sends_through_the_helper():
    plays = yaml.safe_load((ANSIBLE_DIR / "playbooks" / "validate.yml").read_text())
    [task] = [t for p in plays for t in p.get("tasks", [])
              if t.get("register") == "_kc_send_verify"]
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[1].endswith("/helpers/keycloak_send_verify_email.py")
    assert task["failed_when"] is False, "the next tasks surface the failure with the log"
