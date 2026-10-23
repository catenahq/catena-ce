"""The copies of Catena's own services' data stay out of the snapshot.

/var/lib/catena is backed up, and catena-stack-update-managed files the data
it copies before an image moves under /var/lib/catena/prebump/infra-<run>/.
Those copies include Beszel's hub history, which the backup leaves out of the
snapshot on purpose (disposable telemetry that defeats restic's dedup), and
the other services' volumes; a copy in the snapshot would bring them back in
and make the pruning of the copies wait on a backup that holds them.

Run: uv run pytest tests/unit/test_backup_excludes_infra_data_copies.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULTS = (
    Path(__file__).resolve().parents[2]
    / "reconcile" / "roles" / "backup" / "defaults" / "main.yml"
)


def test_the_infra_data_copies_and_their_records_are_excluded():
    patterns = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))[
        "backup_exclude_patterns"]
    assert "*/prebump/infra-*" in patterns, (
        "the copies under /var/lib/catena/prebump/infra-<run> would enter the "
        "snapshot, Beszel's hub history with them"
    )
    assert "*/prebump/infra-*/*" in patterns


def test_the_application_update_copies_are_not_excluded_by_it():
    """The copies of an application update (prebump/<stack>-*.tar) are the
    only way back from a failed rollback; the pattern names infra- alone."""
    patterns = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))[
        "backup_exclude_patterns"]
    assert not [p for p in patterns if "prebump" in p and "infra-" not in p]
