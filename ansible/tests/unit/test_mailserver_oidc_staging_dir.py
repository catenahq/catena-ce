"""The shared staging dir is created for EITHER mailserver consumer.

THE DEFECT. `/var/lib/catena/mailserver` is staging for two different
containers: dovecot-oauth2.conf.ext for dms, oauth.inc.php for roundcube. It
was created gated on the dms container alone, while the Roundcube render that
writes into it is gated on the roundcube container.

Those lifecycles are independent, and one of them is deliberately unstable.
docker-mailserver polls 120s for a mailbox and shuts down without one, so
until a converge gives it a cert and a first mailbox it restarts forever and
`docker ps` returns nothing for it between attempts. Roundcube, meanwhile,
comes up healthy in seconds.

So "roundcube present, dms absent" is not an edge case -- it is the normal
state during the first tokenful converge, the one that exists to wire the
deferred mailserver chain. The Roundcube step then failed with

    Destination directory /var/lib/catena/mailserver does not exist

which aborted the whole converge that was about to fix dms. Observed on bench
run 2026-08-25T18-01-29-b726, cf_activate stage-3b.

Run: uv run pytest tests/unit/test_mailserver_oidc_staging_dir.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
OIDC = ANSIBLE / "roles" / "infrastructure" / "tasks" / "mailserver_oidc.yml"

STAGING = "/var/lib/catena/mailserver"


def _tasks() -> list[dict]:
    return [t for t in (yaml.safe_load(OIDC.read_text()) or [])
            if isinstance(t, dict)]


def _by_name(needle: str) -> dict:
    for t in _tasks():
        if needle.lower() in str(t.get("name", "")).lower():
            return t
    raise AssertionError(f"no task matching {needle!r} in {OIDC}")


def _when(task: dict) -> str:
    w = task.get("when", "")
    return " ".join(w) if isinstance(w, list) else str(w)


def test_the_staging_dir_task_still_exists() -> None:
    """Guard the guard: a rename would make everything below vacuous."""
    task = _by_name("ensure host staging dir")
    assert task["ansible.builtin.file"]["path"] == STAGING


# The exact variable names, because a substring test is not a test here:
# `_dms_ct` is a substring of `mailserver_dms_ct`, so asserting the short form
# passes on a file that never mentions it.
DMS_VAR = "mailserver_dms_ct"
ROUNDCUBE_VAR = "_roundcube_ct"


def test_the_dir_is_created_for_either_consumer() -> None:
    """The property. Gating creation on dms while a roundcube-gated task
    writes into it is the bug."""
    cond = _when(_by_name("ensure host staging dir"))
    assert DMS_VAR in cond, cond
    assert ROUNDCUBE_VAR in cond, cond
    assert " or " in cond, (
        f"the dir must be created when EITHER consumer is present: {cond}")


def test_the_roundcube_render_is_still_gated_on_roundcube() -> None:
    """It writes a roundcube config; gating it on dms would be the mirror of
    the same mistake, skipping real work whenever dms is mid-restart."""
    task = _by_name("render roundcube oauth.inc.php")
    assert task["ansible.builtin.template"]["dest"].startswith(STAGING)
    cond = _when(task)
    assert ROUNDCUBE_VAR in cond
    assert DMS_VAR not in cond


def test_every_writer_into_the_staging_dir_is_covered_by_the_creation_gate() -> None:
    """Whoever writes into the dir must be a consumer the creation gate names.
    A third writer gated on something else would reintroduce this defect
    without touching either task above.
    """
    creation = _when(_by_name("ensure host staging dir"))
    for task in _tasks():
        tmpl = task.get("ansible.builtin.template") or {}
        dest = str(tmpl.get("dest", ""))
        if not dest.startswith(STAGING):
            continue
        cond = _when(task)
        gates = [g for g in (DMS_VAR, ROUNDCUBE_VAR) if g in cond]
        assert gates, f"{task.get('name')!r} writes into {STAGING} ungated"
        for g in gates:
            assert g in creation, (
                f"{task.get('name')!r} is gated on {g}, which the staging-dir "
                f"creation does not consider: {creation}")
