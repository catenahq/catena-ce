"""The backup wrapper arrives with the payload, which is not always first.

roles/backup stopped shipping catena-backup-run on 2026-08-03; it is a lane
script in the catena-admin image payload now. That introduced an ordering the
role never had to think about before, because it used to copy the wrapper
itself and so the file was always there by the time the units were written.

Two hosts, two truths:

  converge owns the payload   roles/payload extracted the engines four roles
                              earlier. A missing wrapper means the extract
                              FAILED, and writing a timer that points at nothing
                              would turn that into a silent unit-level failure
                              at 03:00. Stop the converge.

  payload staged out of band  CATENA_PAYLOAD_INSTALL=false. The bench builds
                              local/catena-admin:bench on the VPS and installs
                              the payload at a later stage, so at THIS point the
                              wrapper legitimately does not exist yet.

The first version of the guard had only the first branch and failed the bench's
very first converge (run 2026-08-03T21-33-27-7a33, stage-1). Deferring is safe
because this role enables NOTHING -- `catena-schedule apply` owns enable/disable
for every lane -- so a host in the gap has a disabled timer pointing at a path
that is about to exist.

The inline first snapshot has to respect the same fact: it starts the unit
synchronously, so with no wrapper it fails the converge for the same reason.

Run: uv run pytest tests/unit/test_backup_wrapper_payload_sequencing.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INSTALL = ANSIBLE / "roles" / "backup" / "tasks" / "install.yml"

EXPECTED = "catena_payload_engines_expected"


def _tasks() -> list[dict]:
    return yaml.safe_load(INSTALL.read_text())


def _find(fragment: str) -> dict:
    for t in _tasks():
        if fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task not found: {fragment!r}")


def _when(task: dict) -> str:
    when = task.get("when")
    if isinstance(when, list):
        return " ".join(str(c) for c in when)
    return str(when)


def test_a_converge_that_owns_the_payload_fails_on_a_missing_wrapper():
    task = _find("backup wrapper is missing from a converge that installs it")
    assert "ansible.builtin.fail" in task
    cond = _when(task)
    assert EXPECTED in cond, (
        "the hard failure is not gated on this converge owning the payload, so "
        "it fires on every out-of-band host too"
    )
    assert "not (_backup_wrapper_stat.stat.exists" in cond


def test_a_host_with_an_out_of_band_payload_defers_instead():
    task = _find("wrapper staged out of band")
    assert "ansible.builtin.debug" in task, (
        "deferring must be a notice, not a failure: the wrapper arrives with "
        "the payload install that follows this converge"
    )
    cond = _when(task)
    assert f"not ({EXPECTED}" in cond
    assert "not (_backup_wrapper_stat.stat.exists" in cond


def test_the_two_branches_are_mutually_exclusive():
    """Both fire, or neither, is a bug either way -- one would double-report and
    the other would leave a missing wrapper completely unremarked."""
    fail_cond = _when(_find("backup wrapper is missing from a converge"))
    defer_cond = _when(_find("wrapper staged out of band"))
    assert f"not ({EXPECTED}" not in fail_cond
    assert f"not ({EXPECTED}" in defer_cond


def test_the_inline_first_snapshot_needs_the_wrapper():
    """It runs `systemctl start --wait catena-backup.service`, so a missing
    wrapper fails the converge at the unit rather than at the guard above."""
    task = _find("Run first backup inline")
    cond = _when(task)
    assert "_backup_wrapper_stat.stat.exists" in cond, (
        "the inline snapshot does not check for the wrapper; on a host whose "
        "payload lands after this role it starts a unit whose ExecStart does "
        "not exist"
    )


def test_the_role_still_installs_the_units_when_the_wrapper_is_absent():
    """The deferral is units-and-config now, binary later. If the unit drops
    were gated on the wrapper too, the later payload install would leave a host
    with a binary and no timer."""
    for name in ("Drop systemd service unit", "Drop systemd daily-backup timer unit"):
        assert "_backup_wrapper_stat" not in _when(_find(name)), (
            f"{name!r} is gated on the wrapper; the units must be written "
            "regardless so the payload install completes a working lane"
        )
