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

Run: uv run pytest tests/unit/test_sovereign_exit_artifacts.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

_ANSIBLE = Path(__file__).resolve().parents[3] / "ansible"
_ROLE = _ANSIBLE / "roles" / "backup"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_INSTALL = _ROLE / "tasks" / "install.yml"
_TEMPLATES = _ROLE / "templates"
_SNAPSHOT_LIST = _ANSIBLE / "scripts" / "snapshot-list.sh"
_SNAPSHOT_EXPORT = _ANSIBLE / "scripts" / "snapshot-export.sh"

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


def test_landing_page_publishes_the_version_stamp():
    body = _SNAPSHOT_LIST.read_text()
    assert "/etc/catena/version.txt" in body, (
        "the recovery landing page must name the version that built this "
        "server -- it is the skew oracle for a rebuild"
    )
    assert "restic dump latest /etc/catena/version.txt" in body, (
        "the page must show how to read the stamp out of a snapshot without "
        "restoring anything"
    )
    assert "RECOVERY-README" in body, "the page must point at the rebuild guide"


def test_export_summary_points_at_the_self_describing_contents():
    body = _SNAPSHOT_EXPORT.read_text()
    assert "./etc/catena/version.txt" in body
    for lang in _LANGS:
        assert f"./etc/catena/RECOVERY-README.{lang}.md" in body, (
            "a downloaded tarball must announce its own rebuild instructions"
        )


def _render_landing_page(tmp_path, version_stamp):
    """Run the script's embedded python block against stub inputs.

    The page body is one big f-string, so an unescaped brace or a bad
    substitution breaks rendering at RUNTIME on a client's box, long after
    review. Rendering it here turns that into a test failure.
    """
    lines = _SNAPSHOT_LIST.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("python3 - "))
    end = next(i for i, l in enumerate(lines) if l.strip() == "PY" and i > start)
    block = tmp_path / "block.py"
    block.write_text("\n".join(lines[start + 1 : end]))

    snaps = tmp_path / "snaps.json"
    snaps.write_text(
        json.dumps(
            [
                {
                    "short_id": "abc123",
                    "time": "2026-07-24T10:00:00Z",
                    "hostname": "dev1",
                    "paths": ["/etc"],
                    "tags": ["weekly"],
                }
            ]
        )
    )
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "snapshot-20260724T100000Z.tar.gz").write_bytes(b"x" * 1024)
    out = tmp_path / "index.html"

    subprocess.run(
        [
            sys.executable,
            str(block),
            str(out),
            str(export_dir),
            "2026-07-24T00:00:00Z",
            "example.com",
            str(snaps),
            version_stamp,
        ],
        check=True,
    )
    return out.read_text()


def test_landing_page_renders_with_the_rebuild_block(tmp_path):
    html = _render_landing_page(tmp_path, "v1.2.3-4-gabcdef\n2026-07-20T00:00:00Z")
    for needle in (
        "v1.2.3-4-gabcdef",
        "restic dump latest /etc/catena/version.txt",
        "RECOVERY-README.en.md",
        "RECOVERY-README.fr.md",
        "abc123",
        "snapshot-20260724T100000Z.tar.gz",
    ):
        assert needle in html, f"missing from the rendered page: {needle}"


def test_landing_page_respects_client_copy_rules(tmp_path):
    """recovery.<zone> is a client surface: no internal vocabulary."""
    html = _render_landing_page(tmp_path, "v1.2.3").lower()
    for banned in ("operator", "playbook", "ansible"):
        assert banned not in html, (
            f"'{banned}' is internal vocabulary and must not appear on a "
            "client-facing page"
        )


def test_landing_page_survives_a_missing_version_stamp(tmp_path):
    """cat of a missing file yields 'unknown'; the page must still render."""
    html = _render_landing_page(tmp_path, "unknown")
    assert "unknown" in html
    assert "Rebuilding this server" in html
