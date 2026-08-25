"""Every bootstrap SSH probe carries the same host-key posture as the
connections it is predicting.

THE DEFECT. bootstrap.yml decides what state a host is in by SSHing to it four
times and reading the exit code. Three of those probes ran on ssh's default
`StrictHostKeyChecking=ask`, which under `BatchMode=yes` cannot ask and simply
fails.

That is only survivable while known_hosts holds an entry for the address, and
by the time these run it does not:

    Phase 0     ssh-keyscan -> known_hosts    (entry written)
    Phase 0.5   helpers/install_key.py        (`ssh-keygen -R` DELETES it, and
                                               both of its own SSH calls use
                                               UserKnownHostsFile=/dev/null, so
                                               nothing writes it back)
    Phase 1     probe, probe, probe           (no entry -> rc=255)

rc=255 with no auth attempted is indistinguishable from "this account cannot
log in" to a caller that only reads the exit code. Two of the three probes read
failure as information and reach a wrong conclusion quietly; the third ABORTS
the install, and did, on a host that answered `true` in 0.1s.

The posture asserted here is the one ansible.cfg already uses for the real
connections (`StrictHostKeyChecking=accept-new`), so a probe and the connection
it predicts agree. accept-new still refuses a CHANGED key -- this is not
host-key checking turned off.

Run: uv run pytest tests/unit/test_bootstrap_probes_are_tofu.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
BOOTSTRAP = ANSIBLE / "playbooks" / "bootstrap.yml"

_TOFU = "StrictHostKeyChecking=accept-new"


def _all_tasks() -> list[dict]:
    """Every task in the playbook, across plays and pre_tasks/tasks/post_tasks."""
    out: list[dict] = []
    for play in yaml.safe_load(BOOTSTRAP.read_text()) or []:
        if not isinstance(play, dict):
            continue
        for section in ("pre_tasks", "tasks", "post_tasks"):
            for task in play.get(section) or []:
                if isinstance(task, dict):
                    out.append(task)
    return out


def _ssh_probes() -> list[tuple[str, str]]:
    """(task name, the text that invokes ssh) for every task that runs ssh
    directly. Both shapes the playbook uses: an argv list and a shell cmd."""
    found: list[tuple[str, str]] = []
    for task in _all_tasks():
        name = str(task.get("name", "<unnamed>"))
        argv = (task.get("ansible.builtin.command") or {})
        if isinstance(argv, dict) and argv.get("argv"):
            items = [str(a) for a in argv["argv"]]
            if items and items[0] == "ssh":
                found.append((name, " ".join(items)))
        shell = (task.get("ansible.builtin.shell") or {})
        if isinstance(shell, dict):
            cmd = str(shell.get("cmd") or "")
            if "ssh -o" in cmd:
                found.append((name, cmd))
    return found


def test_the_playbook_still_probes_by_ssh() -> None:
    """Guard the guard: a rename that stops matching would make every
    assertion below vacuous."""
    probes = _ssh_probes()
    assert len(probes) >= 4, f"expected the four state probes, found {len(probes)}"


def test_every_probe_accepts_a_new_host_key() -> None:
    """The property. A probe on the default `ask` fails closed under BatchMode
    with no entry present, and the caller reads that as 'no access'."""
    missing = [name for name, text in _ssh_probes() if _TOFU not in text]
    assert not missing, (
        "these SSH probes would fail with 'Host key verification failed' on a "
        f"host with no known_hosts entry: {missing}"
    )


def test_no_probe_disables_host_key_checking_outright() -> None:
    """accept-new, not `no` and not /dev/null. A changed key still has to be
    refused: this playbook is what installs the operator's access."""
    for name, text in _ssh_probes():
        assert "StrictHostKeyChecking=no" not in text, name
        assert "UserKnownHostsFile=/dev/null" not in text, name


def test_the_aborting_probe_reports_what_ssh_said() -> None:
    """The probe whose failure ABORTS must carry the reason out with it. Sent
    to /dev/null, a host-key refusal and a closed port are the same event."""
    aborting = [
        (name, text) for name, text in _ssh_probes()
        if "for user in" in text
    ]
    assert len(aborting) == 1, f"expected one looping probe, got {len(aborting)}"
    _, text = aborting[0]
    assert "2>/dev/null" not in text, "the reason is being discarded"
    assert "2>&1" in text and ">&2" in text, (
        "the probe must capture ssh's own message and re-emit it on stderr"
    )

    play_text = BOOTSTRAP.read_text()
    assert "initial_probe.stderr" in play_text, (
        "the abort message must include what SSH reported"
    )
