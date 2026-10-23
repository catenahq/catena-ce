"""Lock the sovereign-exit guarantee: a snapshot explains itself.

The promise is that a client can rebuild from a snapshot with restic +
docker + psql or mariadb alone, without our software and without us. Three artifacts
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
    """/etc is in the product's own backup paths -- that is the whole reason
    for this path. Held against that literal rather than against backup_paths,
    which is an expression adding whatever the client declared and does not
    resolve outside a converge."""
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
    assert "/etc" in defaults["backup_product_paths"], (
        "backup_paths lost /etc -- the README and version stamp would stop "
        "riding snapshots and the sovereign-exit promise would silently break"
    )


def test_readme_does_not_depend_on_our_own_tooling():
    """A rebuild guide that needs our CLI is not an exit."""
    forbidden = ("catena-installer", "catena recover", "catena rollback",
                 "catena restore", "ansible")
    for lang in _LANGS:
        body = (_TEMPLATES / f"RECOVERY-README.{lang}.md.j2").read_text().lower()
        for token in forbidden:
            assert token not in body, (
                f"RECOVERY-README.{lang} references '{token}'; the manual path "
                "must stand on restic + docker + psql alone"
            )
        for tool in ("restic", "docker", "psql"):
            assert tool in body, f"RECOVERY-README.{lang} never mentions {tool}"


def test_the_database_reload_is_plain_sql_through_psql():
    """Database exports are plain SQL files: the guide names the .sql export
    and reloads it with psql alone, with no decompression step."""
    for lang in _LANGS:
        body = (_TEMPLATES / f"RECOVERY-README.{lang}.md.j2").read_text()
        assert "backup-staging/pg/*.sql`" in body, (
            f"RECOVERY-README.{lang} does not name the .sql export")
        assert "psql -U postgres < <" in body, (
            f"RECOVERY-README.{lang} lost the plain psql reload")
        assert "zcat" not in body, (
            f"RECOVERY-README.{lang} still tells the reader to decompress")


def test_the_guide_reloads_the_mariadb_exports_too():
    """The backup writes MariaDB exports beside the Postgres ones, under
    backup-staging/mysql: the guide names them and reloads them with the
    MariaDB client alone."""
    for lang in _LANGS:
        body = (_TEMPLATES / f"RECOVERY-README.{lang}.md.j2").read_text()
        assert "backup-staging/mysql/*.sql`" in body, (
            f"RECOVERY-README.{lang} does not name the MariaDB exports")
        assert "mariadb -u root -p < <" in body, (
            f"RECOVERY-README.{lang} lost the MariaDB reload")


def test_the_one_application_restore_runs_from_its_folder_alone():
    """The folder under compose/ is the application's definition: the guide
    restores it with the volumes, prepares a bare machine for swarm and deploys
    the folder with its .env loaded. Nothing in it reads Portainer's data."""
    steps = (
        "docker swarm init",
        "docker network create --driver overlay catena-network",
        "docker node update --label-add catena.role=data self",
        "set -a; . ./.env; set +a",
        "docker stack deploy -c docker-compose.yml",
        "/compose/",
    )
    for lang in _LANGS:
        body = (_TEMPLATES / f"RECOVERY-README.{lang}.md.j2").read_text()
        for step in steps:
            assert step in body, f"RECOVERY-README.{lang} lost: {step}"
        assert "portainer" not in body.lower(), (
            f"RECOVERY-README.{lang} sends the reader to Portainer's data")
        assert "docker compose" not in body, (
            f"RECOVERY-README.{lang} starts an application outside swarm")


def test_the_application_folders_ride_every_snapshot():
    """The guide points at {{ storage_mount_point }}/compose, so it has to be
    in the backup's own paths."""
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert "{{ storage_mount_point }}/compose" in defaults["backup_product_paths"]


def test_the_export_directory_is_created_for_the_payload_scripts():
    """catena-snapshot-export and catena-snapshot-list write here, and both
    ship in the image payload -- so the DIRECTORY is this role's job even
    though the writers are not. Group 1000 is what lets the catena-admin
    container list the artifacts for the /recovery tab.

    reconcile/roles/catena-admin creates the same directory two roles earlier, because
    it bind-mounts it and swarm rejects a task whose bind source is missing.
    The group therefore comes from the playbooks' shared group_vars rather than a
    literal here: two literals is how the two creators drift, and the loser's
    ownership is whatever ran last. Assert the resolved value, not the
    spelling."""
    tasks = _install_tasks()
    task = next(
        (t for t in tasks if "snapshot export directory" in (t.get("name") or "")),
        None,
    )
    assert task is not None, "the export directory task went missing"
    spec = task["ansible.builtin.file"]
    assert spec["state"] == "directory"
    assert str(spec["group"]) == "{{ catena_export_dir_group }}"
    shared = yaml.safe_load(
        (_ANSIBLE / "playbooks" / "group_vars" / "all" / "main.yml").read_text())
    assert str(shared["catena_export_dir_group"]) == "1000"
