"""backup.env is reconciled against the store, not written once and left.

The file is written during the FIRST converge, when a deferred-backup install
has no repo and no S3 keys yet. The client then enters them in Settings, which
writes /etc/catena/config.json and re-renders nothing. Only-if-absent therefore
left this file blank forever on the ordinary install path.

Every lane script survived that -- catena-restic-env resolves the store -- so
nothing looked broken: backups ran, the panel reported configured. What stayed
broken was the one path with no catena tooling in it at all, the one the exit
story documents:

    set -a; . /etc/catena/backup.env; set +a; restic snapshots

Caught by the bench (sovereign_exit stage-3, run 3642), which reads snapshots
through exactly that command and got nothing, on a host that was backing up
fine.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
INSTALL = ANSIBLE / "reconcile" / "roles" / "backup" / "tasks" / "install.yml"
ENV_TEMPLATE = ANSIBLE / "reconcile" / "roles" / "backup" / "templates" / "backup.env.j2"


@pytest.fixture(scope="module")
def tasks() -> list[dict]:
    return yaml.safe_load(INSTALL.read_text())


@pytest.fixture(scope="module")
def env_task(tasks) -> dict:
    for t in tasks:
        src = (t.get("ansible.builtin.template") or {}).get("src")
        if src == "backup.env.j2":
            return t
    pytest.fail("reconcile/roles/backup no longer renders backup.env.j2")


def test_a_configured_host_reconciles_the_file_every_converge(env_task):
    """The whole point: a host that got its credentials from the panel must end
    up with them in the file too, without anyone re-running an installer."""
    cond = str(env_task["when"])
    assert "_backup_configured" in cond, (
        f"backup.env is not reconciled for a configured host: when={cond!r}"
    )


def test_an_unconfigured_host_still_writes_it_once(env_task):
    """A deferred install has to get the file (the units reference it), and
    then be left alone -- rewriting the same blanks every converge is churn
    that says nothing."""
    cond = str(env_task["when"])
    assert "stat.exists" in cond, (
        f"backup.env is no longer written on a fresh host: when={cond!r}"
    )


def test_the_reconcile_writes_a_complete_set_or_nothing(env_task):
    """_backup_configured is all four of repo + password + both S3 keys. A
    partial reconcile would replace a working file with half a credential set,
    which is worse than the blank it was fixing."""
    cond = str(env_task["when"])
    assert " or " in cond.lower(), f"when={cond!r}"
    assert "_backup_configured" in cond


def test_the_credentials_still_never_reach_a_log(env_task):
    assert env_task.get("no_log") is True


def test_the_template_reads_the_store_backed_values():
    """backup_restic_repo resolves from cfg_backup_restic_repo, which the
    on-box loader publishes from the store. If the template ever hardcodes a
    .env-only value again, the reconcile would write staleness confidently."""
    body = ENV_TEMPLATE.read_text()
    assert "RESTIC_REPOSITORY={{ backup_restic_repo }}" in body
    defaults = yaml.safe_load(
        (ANSIBLE / "reconcile" / "roles" / "backup" / "defaults" / "main.yml").read_text())
    assert "cfg_backup_restic_repo" in str(defaults["backup_restic_repo"])
