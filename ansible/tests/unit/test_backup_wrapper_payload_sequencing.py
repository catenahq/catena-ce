"""The backup wrapper arrives with the payload, which is not always first.

reconcile/roles/backup stopped shipping catena-backup-run on 2026-08-03; it is a lane
script in the catena-admin image payload now. That introduced an ordering the
role never had to think about before, because it used to copy the wrapper
itself and so the file was always there by the time the units were written.

Two hosts, two truths:

  converge owns the payload   reconcile/roles/payload extracted the engines four roles
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
INSTALL = ANSIBLE / "reconcile" / "roles" / "backup" / "tasks" / "install.yml"
VALIDATE = ANSIBLE / "reconcile" / "roles" / "backup" / "tasks" / "validate.yml"
# The "is the payload expected here" decision all five callers now share.
SHARED = ANSIBLE / "bootstrap" / "roles" / "common" / "tasks" / "_payload_expected.yml"

EXPECTED = "catena_payload_expected"
MISSING = "catena_payload_missing"

# Every backup script that now arrives with the image payload rather than from
# this role. catena-disk-preflight is deliberately absent: restore.yml calls it
# on a fresh DR box with no docker, so it is still copied here.
PAYLOAD_SCRIPTS = (
    "backup_wrapper_script",
    "backup_coverage_script",
    "backup_restic_env_script",
    "backup_snapshot_list_script",
)


def _tasks(path: Path = INSTALL) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _find(fragment: str, path: Path = INSTALL) -> dict:
    for t in _tasks(path):
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
    assert MISSING in cond


def test_a_host_with_an_out_of_band_payload_defers_instead():
    task = _find("wrapper staged out of band")
    assert "ansible.builtin.debug" in task, (
        "deferring must be a notice, not a failure: the wrapper arrives with "
        "the payload install that follows this converge"
    )
    cond = _when(task)
    assert f"not ({EXPECTED}" in cond
    assert MISSING in cond


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
    assert "_backup_wrapper_present" in cond, (
        "the inline snapshot does not check for the wrapper; on a host whose "
        "payload lands after this role it starts a unit whose ExecStart does "
        "not exist"
    )


def test_validate_does_not_assert_payload_scripts_on_a_role_owned_fixture_list():
    """The always-present list must not name a payload script.

    validate.yml asserted all four in one flat list with the units and restic,
    which failed the bench's stage-1 validate for the same reason the guard did
    -- the payload had not landed yet. The role-owned fixtures and the
    payload-shipped ones now assert separately because they arrive at different
    times."""
    task = _find("role-owned fixtures exist", VALIDATE)
    listed = str(task["vars"]["_assert_paths"])
    for var in PAYLOAD_SCRIPTS:
        assert var not in listed, (
            f"{var} is in validate's always-present fixture list; it ships in "
            "the image payload and can legitimately arrive after a converge"
        )
    # The one script this role still copies must stay asserted unconditionally.
    assert "catena-disk-preflight" in listed


def test_validate_still_asserts_the_payload_scripts_once_any_is_present():
    """Deferring must not become never-checking.

    The gate is "this deployment installs the payload, OR some of it is already
    here". `some` rather than `all` is the point: a host holding three of four
    is a broken payload install, and requiring all four would make that case
    indistinguishable from the not-yet case and skip the only check that would
    have caught it.

    The decision moved into bootstrap/roles/common/tasks/_payload_expected.yml, which is
    where the other four callers now get it too -- it started here, and the
    property is asserted where it lives rather than restated at each caller."""
    shared = _find("payload-expected: decide", SHARED)
    expr = str(shared["ansible.builtin.set_fact"]["catena_payload_expected"])
    assert "CATENA_PAYLOAD_INSTALL" in expr
    assert "catena_payload_engines_expected" in expr, (
        "a converge must prefer reconcile/roles/payload's fact; falling straight to the "
        "env var would answer for the play rather than for this converge"
    )
    assert "catena_payload_present | length > 0" in expr, (
        "the presence half of the gate requires ALL paths, so a partial "
        "payload install is skipped instead of caught"
    )

    passed = _find("are the payload scripts expected here", VALIDATE)
    assert passed["vars"]["_payload_paths"] == [
        "{{ backup_wrapper_script }}",
        "{{ backup_coverage_script }}",
        "{{ backup_restic_env_script }}",
        "{{ backup_snapshot_list_script }}",
    ], "all four, or the partial-install case cannot be seen"

    assertion = _find("payload-shipped scripts installed + executable", VALIDATE)
    assert "_bk_payload_expected" in str(assertion["when"])
    that = str(assertion["ansible.builtin.assert"]["that"])
    assert "item.stat.exists" in that
    assert "0755" in that


def test_the_partial_install_case_is_named_by_the_shared_predicate():
    """`catena_payload_partial` exists so a caller can say "three of four" out
    loud rather than reporting a broken install as a host mid-assembly."""
    shared = _find("payload-expected: decide", SHARED)
    expr = str(shared["ansible.builtin.set_fact"]["catena_payload_partial"])
    assert "catena_payload_present | length > 0" in expr
    assert "catena_payload_missing | length > 0" in expr


def test_validate_gates_the_restic_reachability_probe_on_the_entrypoint():
    """It shells out to catena-restic-env, which is itself a payload script. Left
    ungated it reports on a repository it never managed to ask about."""
    for name in ("restic can reach the repo", "restic cat config exited 0"):
        assert "_bk_payload_expected" in str(_find(name, VALIDATE)["when"])


def test_validate_skips_the_restic_probe_until_backup_is_configured():
    """The repo + S3 keys are entered in catena-admin AFTER the install, so a
    host nobody has configured yet has the units installed and no restic.pass.
    That is the documented steady state of a fresh install, not a fault: the
    probe has to skip, or every install fails validation until someone opens
    the panel.

    The condition must match install.yml's `_backup_configured` exactly. A
    looser one (repo alone) passes on a host holding a repo and no keys, which
    is the half-configured case whose restic call fails for a real reason."""
    gate = str(_find("is this host backup-configured", VALIDATE)
               ["ansible.builtin.set_fact"]["_bk_configured"])
    for cred in ("backup_restic_password", "backup_s3_access_key",
                 "backup_s3_secret_key", "backup_restic_repo"):
        assert cred in gate, f"{cred} missing from the configured-gate"

    for name in ("restic can reach the repo", "restic cat config exited 0"):
        assert "_bk_configured" in str(_find(name, VALIDATE)["when"])


def test_the_role_still_installs_the_units_when_the_wrapper_is_absent():
    """The deferral is units-and-config now, binary later. If the unit drops
    were gated on the wrapper too, the later payload install would leave a host
    with a binary and no timer."""
    for name in ("Drop systemd service unit", "Drop systemd daily-backup timer unit"):
        assert "_backup_wrapper_stat" not in _when(_find(name)), (
            f"{name!r} is gated on the wrapper; the units must be written "
            "regardless so the payload install completes a working lane"
        )
