"""Lock the sovereign-exit guarantee: a snapshot explains itself.

The promise is that a client can rebuild from a snapshot with restic +
docker + psql alone, without our software and without us. Three artifacts
carry that promise, and all three are easy to break silently:

  - RECOVERY-README.{en,fr}.md rendered into /etc/catena, which is in
    backup_paths, so the instructions ride every snapshot and are readable
    with `restic dump` without restoring anything.
  - The recovery landing page naming the version that built the server,
    because rebuilding onto a NEWER version can meet a database format the
    snapshot predates.
  - The export summary pointing at both, so a downloaded tarball is not an
    opaque blob.

A README that references our own CLI would defeat the point (the client
would still depend on us), so that is asserted too.

The other two artifacts -- the recovery landing page and the export summary --
are rendered by scripts that now ship in the catena-admin image payload, so
their assertions moved to catena-admin payload/lanes/snapshot_page_test.go.
Both halves matter and neither is sufficient: a page that names the version
stamp is useless if /etc stops riding snapshots, and /etc riding snapshots is
useless if the page never says how to read it. This file owns the second half.

Run: uv run pytest tests/unit/test_sovereign_exit_artifacts.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[3] / "ansible"
_ROLE = _ANSIBLE / "reconcile" / "roles" / "backup"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_INSTALL = _ROLE / "tasks" / "install.yml"
_TEMPLATES = _ROLE / "templates"

_LANGS = ("en", "fr")


def _install_tasks():
    return yaml.safe_load(_INSTALL.read_text())


def test_readme_templates_exist_in_both_languages():
    for lang in _LANGS:
        tpl = _TEMPLATES / f"RECOVERY-README.{lang}.md.j2"
        assert tpl.is_file(), f"missing {tpl.name}; EN/FR parity is not optional"


def test_readme_rendered_into_etc_catena():
    """/etc is in backup_paths -- that is the whole reason for this path."""
    tasks = _install_tasks()
    task = next(
        (t for t in tasks if "manual-rebuild README" in (t.get("name") or "")),
        None,
    )
    assert task is not None, "the README render task went missing"
    dest = task["ansible.builtin.template"]["dest"]
    assert dest.startswith("/etc/catena/"), (
        "the README must live under /etc so it rides every snapshot; "
        f"got {dest}"
    )
    assert sorted(task["loop"]) == sorted(_LANGS)

    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert "/etc" in defaults["backup_paths"], (
        "backup_paths lost /etc -- the README and version stamp would stop "
        "riding snapshots and the sovereign-exit promise would silently break"
    )


def test_readme_does_not_depend_on_our_own_tooling():
    """A rebuild guide that needs our CLI is not an exit."""
    forbidden = ("catena recover", "catena rollback", "catena restore", "ansible")
    for lang in _LANGS:
        body = (_TEMPLATES / f"RECOVERY-README.{lang}.md.j2").read_text().lower()
        for token in forbidden:
            assert token not in body, (
                f"RECOVERY-README.{lang} references '{token}'; the manual path "
                "must stand on restic + docker + psql alone"
            )
        for tool in ("restic", "docker", "psql"):
            assert tool in body, f"RECOVERY-README.{lang} never mentions {tool}"


def test_the_export_directory_is_created_for_the_payload_scripts():
    """catena-snapshot-export and catena-snapshot-list write here, and both
    ship in the image payload -- so the DIRECTORY is this role's job even
    though the writers are not. Group 1000 is what lets the catena-admin
    container list the artifacts for the /recovery tab.

    reconcile/roles/catena-admin creates the same directory two roles earlier, because
    it bind-mounts it and swarm rejects a task whose bind source is missing.
    The group therefore comes from bootstrap/roles/common rather than a literal here:
    two literals is how the two creators drift, and the loser's ownership is
    whatever ran last. Assert the resolved value, not the spelling."""
    tasks = _install_tasks()
    task = next(
        (t for t in tasks if "snapshot export directory" in (t.get("name") or "")),
        None,
    )
    assert task is not None, "the export directory task went missing"
    spec = task["ansible.builtin.file"]
    assert spec["state"] == "directory"
    assert str(spec["group"]) == "{{ catena_export_dir_group }}"
    common = yaml.safe_load(
        (_ANSIBLE / "bootstrap" / "roles" / "common" / "defaults" / "main.yml").read_text())
    assert str(common["catena_export_dir_group"]) == "1000"
